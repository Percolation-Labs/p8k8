"""Push notification relay — APNs (iOS) + FCM (Android).

===============================================================================
SETUP & ARCHITECTURE
===============================================================================

Both APNs and FCM are free and unlimited.

1. APNs (iOS)
   We enabled "Apple Push Notifications service (APNs)" on the existing
   Sign In with Apple key in Apple Developer Console. Same .p8 key, same
   key ID, same team ID — no new credentials. Just added:

       P8_APNS_BUNDLE_ID=com.co.app
       P8_APNS_ENVIRONMENT=production          # sandbox for dev/TestFlight

2. FCM (Android)
   We enabled Firebase Cloud Messaging API on the existing Google Cloud
   project, then downloaded a service account JSON from
   IAM → Service Accounts → Keys → Create new key → JSON. Added:

       P8_FCM_PROJECT_ID=XXXXXXX
       P8_FCM_SERVICE_ACCOUNT_FILE=/secrets/fcm/fcm-service-account.json

   On K8s the JSON is mounted as a secret volume at /secrets/fcm/.

   also firebase andriod app downloaded google.json file for app in the project with that project id from google cloud console

3. Device registration
   The Flutter app uses firebase_messaging to get push tokens from both
   platforms. After login, it calls PATCH /auth/me with the token:

       {"devices": [{"platform": "apns", "token": "...", "device_name": "..."}]}

   Tokens are stored in User.devices JSONB. The app merges new tokens
   with existing ones so multiple devices per user work.

4. Sending & notification moments
   POST /notifications/send reads user.devices and delivers to APNs/FCM.
   Each send also creates a Moment with moment_type='notification' so
   the notification appears in the user's feed.

   Auto-deactivation: APNs 410 or FCM UNREGISTERED → token marked
   {"active": false} on the user record.

5. pg_cron scheduled sends
   pg_cron + pg_net call POST /notifications/send on a schedule.
   The API is network-locked inside the K8s cluster (ClusterIP service).

       -- Store API key as Postgres GUC
       ALTER DATABASE p8 SET p8.api_key = 'your-key';

       -- Daily digest at 9 AM UTC
       SELECT cron.schedule('daily-digest', '0 9 * * *', $$
           SELECT net.http_post(
               url := 'http://p8-api.p8.svc:8000/notifications/send',
               headers := jsonb_build_object(
                   'Authorization', 'Bearer ' || current_setting('p8.api_key'),
                   'Content-Type', 'application/json'
               ),
               body := jsonb_build_object(
                   'user_ids', (SELECT jsonb_agg(id::text) FROM users
                                WHERE deleted_at IS NULL AND devices != '[]'::jsonb),
                   'title', 'Your Daily Digest',
                   'body', 'Here is what happened yesterday...'
               )
           );
       $$);

   Manage: SELECT * FROM cron.job; / SELECT cron.unschedule('daily-digest');

6. App refresh
   The Flutter app polls the feed every 60 seconds when in the foreground
   so new notification moments appear without manual pull-to-refresh.
   Push notifications also trigger an immediate refresh.

===============================================================================
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx

from p8.services.database import Database
from p8.settings import Settings
from p8.utils.parsing import ensure_parsed

logger = logging.getLogger(__name__)

# APNs endpoints
_APNS_PRODUCTION = "https://api.push.apple.com"
_APNS_SANDBOX = "https://api.sandbox.push.apple.com"

# FCM v1 endpoint template
_FCM_V1_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

# APNs provider JWT is valid for up to 60 minutes; refresh at 58 min
_APNS_JWT_TTL = 58 * 60


class NotificationService:
    """Send push notifications to iOS (APNs) and Android (FCM) devices.

    Device tokens live on User.devices (JSONB array). This service reads
    them directly — no separate device_tokens table.

    Each send creates a Moment with moment_type='notification' so the
    notification appears in the user's feed alongside other moments.
    """

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

        # APNs state
        self._apns_enabled = bool(settings.apns_bundle_id and settings.apple_private_key_path)
        self._apns_jwt: str | None = None
        self._apns_jwt_issued_at: float = 0
        self._apns_key: object | None = None

        # FCM state
        self._fcm_enabled = bool(settings.fcm_project_id and settings.fcm_service_account_file)
        self._fcm_credentials: object | None = None

        # Shared HTTP clients (created lazily)
        self._apns_client: httpx.AsyncClient | None = None
        self._fcm_client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_to_user(
        self,
        user_id: UUID,
        title: str,
        body: str,
        data: dict | None = None,
    ) -> list[dict]:
        """Send a push notification to all active devices for a user.

        Also creates a Moment with moment_type='notification' so the
        notification appears in the user's feed.

        Returns result per device. Auto-deactivates tokens on 410/UNREGISTERED.
        """
        row = await self._db.fetchrow(
            "SELECT devices, tenant_id FROM users WHERE id = $1 AND deleted_at IS NULL",
            user_id,
        )
        if not row:
            logger.warning("notification: user %s not found or deleted", user_id)
            return []

        # Create a notification moment in the user's feed
        await self._create_notification_moment(user_id, title, body, data, row.get("tenant_id"))

        devices = ensure_parsed(row["devices"], default=[])

        if not devices:
            logger.info("notification: user %s has no registered devices", user_id)
            return []

        results = []
        deactivated_tokens: list[str] = []

        for device in devices:
            if not device.get("active", True):
                continue
            platform = device.get("platform")
            token = device.get("token")
            if not platform or not token:
                continue

            try:
                if platform == "apns":
                    result = await self._send_apns(token, title, body, data)
                elif platform == "fcm":
                    result = await self._send_fcm(token, title, body, data)
                else:
                    result = {"status": "error", "error": f"Unknown platform: {platform}"}

                if result.get("deactivate"):
                    deactivated_tokens.append(token)
                    result["deactivated"] = True

                result["token"] = token
                result["platform"] = platform
                results.append(result)

            except Exception as exc:
                results.append({
                    "token": token,
                    "platform": platform,
                    "status": "error",
                    "error": str(exc),
                })

        # Batch-deactivate invalid tokens on the user record
        if deactivated_tokens:
            await self._deactivate_tokens(user_id, deactivated_tokens)

        return results

    async def close(self) -> None:
        """Close HTTP clients."""
        if self._apns_client:
            await self._apns_client.aclose()
        if self._fcm_client:
            await self._fcm_client.aclose()

    # ------------------------------------------------------------------
    # Notification moments
    # ------------------------------------------------------------------

    async def _create_notification_moment(
        self,
        user_id: UUID,
        title: str,
        body: str,
        data: dict | None,
        tenant_id: str | None,
    ) -> None:
        """Create a Moment with moment_type='notification' so it shows in the feed."""
        moment_id = uuid4()
        now = datetime.now(timezone.utc)
        await self._db.execute(
            """
            INSERT INTO moments (id, name, moment_type, summary, starts_timestamp,
                                 ends_timestamp, user_id, tenant_id, metadata)
            VALUES ($1, $2, 'notification', $3, $4, $4, $5, $6, $7)
            """,
            moment_id,
            title,
            body,
            now,
            user_id,
            tenant_id,
            json.dumps(data) if data else "{}",
        )

    # ------------------------------------------------------------------
    # APNs transport
    # ------------------------------------------------------------------

    def _get_apns_key(self):
        """Load the ES256 private key from the .p8 file (once)."""
        if self._apns_key is None:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            with open(self._settings.apple_private_key_path, "rb") as f:
                self._apns_key = load_pem_private_key(f.read(), password=None)
        return self._apns_key

    def _get_apns_jwt(self) -> str:
        """Create or return a cached APNs provider JWT (ES256, ~58 min TTL)."""
        now = time.time()
        if self._apns_jwt and (now - self._apns_jwt_issued_at) < _APNS_JWT_TTL:
            return self._apns_jwt

        import jwt as pyjwt

        key = self._get_apns_key()
        payload = {"iss": self._settings.apple_team_id, "iat": int(now)}
        self._apns_jwt = pyjwt.encode(
            payload, key, algorithm="ES256",
            headers={"kid": self._settings.apple_key_id},
        )
        self._apns_jwt_issued_at = now
        return self._apns_jwt

    async def _get_apns_client(self) -> httpx.AsyncClient:
        if self._apns_client is None:
            self._apns_client = httpx.AsyncClient(http2=True, timeout=30.0)
        return self._apns_client

    async def _send_apns(
        self, token: str, title: str, body: str, data: dict | None = None,
    ) -> dict:
        if not self._apns_enabled:
            return {"status": "skipped", "error": "APNs not configured"}

        base_url = (
            _APNS_PRODUCTION
            if self._settings.apns_environment == "production"
            else _APNS_SANDBOX
        )
        url = f"{base_url}/3/device/{token}"

        jwt_token = self._get_apns_jwt()
        headers = {
            "authorization": f"bearer {jwt_token}",
            "apns-topic": self._settings.apns_bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
        if data:
            payload.update(data)

        client = await self._get_apns_client()
        resp = await client.post(url, json=payload, headers=headers)

        apns_id = resp.headers.get("apns-id", "")

        if resp.status_code == 200:
            return {"status": "delivered", "apns_id": apns_id}

        result: dict = {"status": "error", "error": resp.text, "apns_id": apns_id}
        if resp.status_code == 410:
            result["deactivate"] = True
        return result

    # ------------------------------------------------------------------
    # FCM transport
    # ------------------------------------------------------------------

    def _get_fcm_credentials(self):
        """Load Google service account credentials (once) with FCM scope."""
        if self._fcm_credentials is None:
            from google.oauth2 import service_account
            self._fcm_credentials = service_account.Credentials.from_service_account_file(
                self._settings.fcm_service_account_file,
                scopes=["https://www.googleapis.com/auth/firebase.messaging"],
            )
        return self._fcm_credentials

    async def _get_fcm_access_token(self) -> str:
        from google.auth.transport.requests import Request as GoogleRequest
        creds = self._get_fcm_credentials()
        if not creds.valid:
            creds.refresh(GoogleRequest())
        return creds.token  # type: ignore[no-any-return]

    async def _get_fcm_client(self) -> httpx.AsyncClient:
        if self._fcm_client is None:
            self._fcm_client = httpx.AsyncClient(timeout=30.0)
        return self._fcm_client

    async def _send_fcm(
        self, token: str, title: str, body: str, data: dict | None = None,
    ) -> dict:
        if not self._fcm_enabled:
            return {"status": "skipped", "error": "FCM not configured"}

        url = _FCM_V1_URL.format(project_id=self._settings.fcm_project_id)
        access_token = await self._get_fcm_access_token()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        message: dict = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
            }
        }
        if data:
            message["message"]["data"] = {k: str(v) for k, v in data.items()}

        client = await self._get_fcm_client()
        resp = await client.post(url, json=message, headers=headers)

        if resp.status_code == 200:
            resp_data = resp.json()
            return {"status": "delivered", "fcm_message_id": resp_data.get("name", "")}

        result: dict = {"status": "error", "error": resp.text}
        try:
            err_detail = resp.json()
            error_code = (
                err_detail.get("error", {})
                .get("details", [{}])[0]
                .get("errorCode", "")
            )
            if error_code == "UNREGISTERED":
                result["deactivate"] = True
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------
    # Token deactivation (writes back to user.devices)
    # ------------------------------------------------------------------

    async def _deactivate_tokens(self, user_id: UUID, tokens: list[str]) -> None:
        """Mark device tokens as inactive on the user record."""
        logger.info("Deactivating %d token(s) for user %s", len(tokens), user_id)
        await self._db.execute(
            """
            UPDATE users SET devices = (
                SELECT COALESCE(jsonb_agg(
                    CASE WHEN d->>'token' = ANY($2)
                         THEN d || '{"active": false}'::jsonb
                         ELSE d
                    END
                ), '[]'::jsonb)
                FROM jsonb_array_elements(devices) AS d
            ), updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            """,
            user_id, tokens,
        )
