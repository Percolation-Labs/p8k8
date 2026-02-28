"""Email service — Microsoft Graph API + SMTP + Resend + console fallback.

===============================================================================
SETUP
===============================================================================

1. Microsoft Graph API (recommended for M365 / GoDaddy email)
   Best approach when SMTP basic auth is disabled or 2FA is enabled.
   Uses OAuth2 client credentials — no user interaction needed.

   a. Register an app in Azure Portal → App registrations
   b. API permissions → Add → Microsoft Graph → Application → Mail.Send
   c. Click "Grant admin consent" (requires Global Admin)
   d. Certificates & secrets → New client secret → copy the Value
   e. Set env vars:

       P8_EMAIL_PROVIDER=microsoft_graph
       P8_EMAIL_FROM=saoirse@percolationlabs.ai
       P8_MS_GRAPH_TENANT_ID=a1241439-43fb-4a97-9bc7-e2e9d6d7291d
       P8_MS_GRAPH_CLIENT_ID=10cfa005-f60c-408e-97ea-0dcaf5b2337d
       P8_MS_GRAPH_CLIENT_SECRET=<client-secret-value>

2. SMTP (any SMTP server — may not work with M365 if basic auth disabled)
       P8_EMAIL_PROVIDER=smtp
       P8_SMTP_HOST=smtp.office365.com
       P8_SMTP_PORT=587
       P8_SMTP_USERNAME=user@domain.com
       P8_SMTP_PASSWORD=<password-or-app-password>

3. Resend (API-based, no SMTP)
       P8_EMAIL_PROVIDER=resend
       P8_RESEND_API_KEY=re_...

4. Console (dev/test — prints to stdout)
       P8_EMAIL_PROVIDER=console

===============================================================================
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from p8.settings import Settings

logger = logging.getLogger(__name__)

# Microsoft Graph token endpoint + send mail API
_MS_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_MS_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/users/{user_email}/sendMail"


class EmailService:
    """Send transactional emails via Microsoft Graph, SMTP, Resend, or console."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._provider = settings.email_provider  # console | smtp | resend | microsoft_graph
        # Cached Graph API access token
        self._ms_token: str | None = None
        self._ms_token_expires: float = 0

    @property
    def enabled(self) -> bool:
        """True if a real email backend is configured (not console)."""
        if self._provider == "microsoft_graph":
            s = self._settings
            return bool(s.ms_graph_tenant_id and s.ms_graph_client_id and s.ms_graph_client_secret)
        if self._provider == "smtp":
            return bool(self._settings.smtp_host and self._settings.smtp_username)
        if self._provider == "resend":
            return bool(self._settings.resend_api_key)
        return False

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        html: str | None = None,
        from_addr: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
    ) -> dict:
        """Send an email. Returns {"status": "sent"|"logged", ...}.

        Args:
            to: Recipient email (comma-separated for multiple).
            subject: Email subject line.
            body: Plain-text body.
            html: Optional HTML body (sent as alternative part).
            from_addr: Override the default P8_EMAIL_FROM.
            cc: CC recipients (comma-separated).
            bcc: BCC recipients (comma-separated).
        """
        sender = from_addr or self._settings.email_from

        if self._provider == "microsoft_graph":
            return await self._send_graph(to, subject, body, html=html, sender=sender, cc=cc, bcc=bcc)
        elif self._provider == "smtp":
            return await self._send_smtp(to, subject, body, html=html, sender=sender, cc=cc, bcc=bcc)
        elif self._provider == "resend":
            return await self._send_resend(to, subject, body, html=html, sender=sender, cc=cc, bcc=bcc)
        else:
            return self._send_console(to, subject, body, sender=sender)

    # ------------------------------------------------------------------
    # Microsoft Graph API (OAuth2 client credentials)
    # ------------------------------------------------------------------

    async def _get_graph_token(self) -> str:
        """Get or refresh an OAuth2 access token for Microsoft Graph."""
        import time

        if self._ms_token and time.time() < self._ms_token_expires:
            return self._ms_token

        s = self._settings
        url = _MS_TOKEN_URL.format(tenant_id=s.ms_graph_tenant_id)

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data={
                "grant_type": "client_credentials",
                "client_id": s.ms_graph_client_id,
                "client_secret": s.ms_graph_client_secret,
                "scope": "https://graph.microsoft.com/.default",
            })
            resp.raise_for_status()
            data = resp.json()

        self._ms_token = data["access_token"]
        # Expire 5 min early to avoid edge cases
        self._ms_token_expires = time.time() + data.get("expires_in", 3600) - 300
        return self._ms_token

    async def _send_graph(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        html: str | None = None,
        sender: str,
        cc: str | None = None,
        bcc: str | None = None,
    ) -> dict:
        token = await self._get_graph_token()
        url = _MS_SEND_MAIL_URL.format(user_email=sender)

        # Build recipients
        def _recipients(addrs: str) -> list[dict]:
            return [{"emailAddress": {"address": a.strip()}} for a in addrs.split(",") if a.strip()]

        content_type = "HTML" if html else "Text"
        content = html if html else body

        message: dict = {
            "subject": subject,
            "body": {"contentType": content_type, "content": content},
            "toRecipients": _recipients(to),
        }
        if cc:
            message["ccRecipients"] = _recipients(cc)
        if bcc:
            message["bccRecipients"] = _recipients(bcc)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"message": message, "saveToSentItems": True},
            )
            if resp.status_code == 202:
                logger.info("Email sent via Microsoft Graph to %s: %s", to, subject)
                return {"status": "sent", "provider": "microsoft_graph", "to": to}

            # Log error details for debugging
            logger.error("Graph API send failed (%d): %s", resp.status_code, resp.text)
            resp.raise_for_status()
            return {"status": "error", "provider": "microsoft_graph"}  # unreachable

    # ------------------------------------------------------------------
    # SMTP (any SMTP server)
    # ------------------------------------------------------------------

    async def _send_smtp(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        html: str | None = None,
        sender: str,
        cc: str | None = None,
        bcc: str | None = None,
    ) -> dict:
        s = self._settings
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc

        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")

        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as server:
                server.starttls()
                if s.smtp_username:
                    server.login(s.smtp_username, s.smtp_password)
                server.send_message(msg)
            logger.info("Email sent via SMTP to %s: %s", to, subject)
            return {"status": "sent", "provider": "smtp", "to": to}
        except Exception as exc:
            logger.error("SMTP send failed to %s: %s", to, exc)
            raise

    # ------------------------------------------------------------------
    # Resend (API)
    # ------------------------------------------------------------------

    async def _send_resend(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        html: str | None = None,
        sender: str,
        cc: str | None = None,
        bcc: str | None = None,
    ) -> dict:
        s = self._settings
        payload: dict = {
            "from": sender,
            "to": [addr.strip() for addr in to.split(",")],
            "subject": subject,
            "text": body,
        }
        if html:
            payload["html"] = html
        if cc:
            payload["cc"] = [addr.strip() for addr in cc.split(",")]
        if bcc:
            payload["bcc"] = [addr.strip() for addr in bcc.split(",")]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {s.resend_api_key}"},
                json=payload,
            )
            resp.raise_for_status()

        logger.info("Email sent via Resend to %s: %s", to, subject)
        return {"status": "sent", "provider": "resend", "to": to}

    # ------------------------------------------------------------------
    # Console (dev/test)
    # ------------------------------------------------------------------

    def _send_console(self, to: str, subject: str, body: str, *, sender: str) -> dict:
        logger.info("Email [console] from=%s to=%s subject=%s", sender, to, subject)
        print(f"\n--- Email ---\nFrom: {sender}\nTo: {to}\nSubject: {subject}\n\n{body}\n---\n")  # noqa: T201
        return {"status": "logged", "provider": "console", "to": to}
