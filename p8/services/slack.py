"""Slack service — message posting, thread reading, user resolution, alerts.

Includes SlackAlertHandler (logging.Handler) that forwards ERROR+ to Slack.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any
from uuid import UUID

from pydantic import BaseModel, model_validator
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.signature import SignatureVerifier

from p8.ontology.base import deterministic_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SlackFiles(BaseModel):
    name: str | None = None
    title: str | None = None
    url_private_download: str | None = None
    mimetype: str | None = None
    filetype: str | None = None


class SlackThread(BaseModel):
    ts: str
    text: str
    files: list[SlackFiles] = []
    user: str | None = "unknown"


class SlackMessage(BaseModel):
    channel_id: str | None = None
    channel: str | None = None
    ts: str
    thread_ts: str | None = None
    text: str
    reply_count: int = 0
    user: str | None = "unknown"
    files: list[SlackFiles] = []
    replies: list[SlackThread] = []
    type: str | None = None
    bot_id: str | None = None

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def normalize_channel(cls, values: dict) -> dict:
        channel = values.get("channel_id") or values.get("channel")
        values["channel"] = values["channel_id"] = channel
        return values

    @property
    def thread_id(self) -> str:
        return self.thread_ts or self.ts


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SlackService:
    """Thin wrapper around Slack WebClient for p8 integration."""

    def __init__(self, db, settings):
        from p8.settings import Settings

        self._db = db
        self._settings: Settings = settings
        self._client = WebClient(token=settings.slack_bot_token)
        self._verifier = (
            SignatureVerifier(signing_secret=settings.slack_signing_secret)
            if settings.slack_signing_secret
            else None
        )
        self._bot_user_id: str | None = None
        self._email_cache: dict[str, str] = {}
        self._channel_id_cache: dict[str, str] = {}

    # -- signature verification ---------------------------------------------

    def verify_request(self, body: bytes, timestamp: str, signature: str) -> bool:
        """Verify Slack request signature."""
        if not self._verifier:
            return True
        return self._verifier.is_valid(body, timestamp, signature)

    # -- messaging ----------------------------------------------------------

    def post_message(
        self,
        text: str,
        channel: str,
        thread_ts: str | None = None,
        use_markdown: bool = False,
    ) -> Any:
        return self._client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=str(text),
            mrkdwn=use_markdown,
        )

    def update_message(
        self,
        text: str,
        channel: str,
        ts: str,
        use_markdown: bool = False,
    ) -> Any:
        return self._client.chat_update(
            channel=channel,
            ts=ts,
            text=str(text),
            mrkdwn=use_markdown,
        )

    def get_thread(self, channel_id: str, thread_ts: str, limit: int = 100) -> list[dict[str, str]]:
        result = self._client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=limit,
        )
        bot_uid = self._get_bot_user_id()
        messages: list[dict[str, str]] = []
        data = result.data
        assert isinstance(data, dict)
        for msg in data["messages"]:
            if msg.get("user") == bot_uid or msg.get("bot_id"):
                role = "assistant"
                content = msg.get("text", "")
            else:
                role = "user"
                content = msg.get("text", "")
            messages.append({"role": role, "content": content})
        return messages

    def _get_bot_user_id(self) -> str | None:
        if self._bot_user_id:
            return self._bot_user_id
        try:
            resp = self._client.auth_test()
            data = resp.data
            assert isinstance(data, dict)
            self._bot_user_id = data.get("user_id")
        except SlackApiError:
            pass
        return self._bot_user_id

    # -- alerts -------------------------------------------------------------

    def post_alert(self, text: str, channel: str | None = None) -> Any:
        """Post an alert message to the configured alerts channel."""
        target = channel or self._settings.slack_alerts_channel
        if not target:
            logger.warning("No alerts channel configured, skipping alert")
            return None
        try:
            return self._client.chat_postMessage(channel=target, text=text, mrkdwn=True)
        except SlackApiError as e:
            # Use print to avoid recursion if this logger also has the Slack handler
            print(f"Failed to post alert to {target}: {e}")
            return None

    def upload_file(
        self,
        content: str | bytes,
        filename: str,
        channel: str | None = None,
        initial_comment: str | None = None,
    ) -> Any:
        """Upload a file to a channel (e.g. CSV report)."""
        target = channel or self._settings.slack_alerts_channel
        if not target:
            return None
        try:
            channel_id = self._resolve_channel_id(target)
            data = content.encode("utf-8") if isinstance(content, str) else content
            return self._client.files_upload_v2(
                channel=channel_id,
                file=data,
                filename=filename,
                initial_comment=initial_comment,
            )
        except SlackApiError as e:
            print(f"Failed to upload file to {target}: {e}")
            return None

    def _resolve_channel_id(self, channel: str) -> str:
        """Resolve a channel name to its ID. Pass-through if already an ID."""
        if channel.startswith(("C", "G", "D")) and channel[1:].isalnum():
            return channel
        if channel in self._channel_id_cache:
            return self._channel_id_cache[channel]
        # Post + delete a throwaway message to resolve name → ID
        # (chat_postMessage accepts names; files_upload_v2 does not)
        try:
            resp = self._client.chat_postMessage(
                channel=channel.lstrip("#"), text="\u200b",  # zero-width space
            )
            resp_data = resp.data
            assert isinstance(resp_data, dict)
            cid: str = resp_data["channel"]
            self._channel_id_cache[channel] = cid
            # Clean up the throwaway message
            self._client.chat_delete(channel=cid, ts=resp_data["ts"])
            return cid
        except SlackApiError as e:
            logger.warning("Could not resolve channel '%s': %s", channel, e)
        return channel

    # -- user resolution ----------------------------------------------------

    def _get_email(self, slack_user_id: str) -> str | None:
        if slack_user_id in self._email_cache:
            return self._email_cache[slack_user_id]
        try:
            resp = self._client.users_info(user=slack_user_id)
            if resp["ok"]:
                user_data: dict[str, Any] = resp["user"]
                profile: dict[str, Any] = user_data.get("profile", {})
                email: str | None = profile.get("email")
                if email:
                    self._email_cache[slack_user_id] = email
                    return email
        except SlackApiError as e:
            logger.error("Slack API error looking up user %s: %s", slack_user_id, e)
        return None

    def _get_user_name(self, slack_user_id: str) -> str:
        try:
            resp = self._client.users_info(user=slack_user_id)
            if resp["ok"]:
                user_info: dict[str, Any] = resp["user"]
                profile: dict[str, Any] = user_info.get("profile", {})
                return str(
                    profile.get("real_name")
                    or profile.get("display_name")
                    or user_info.get("name")
                    or slack_user_id
                )
        except SlackApiError:
            pass
        return slack_user_id

    async def resolve_user(self, slack_user_id: str) -> tuple[UUID, str]:
        """Resolve Slack user -> (p8 user_id, tenant_id).

        Looks up email via Slack API, generates deterministic_id,
        and ensures a user row exists (via AuthService find-or-create).
        Falls back to deterministic_id even if DB write fails.
        """
        email = self._get_email(slack_user_id)
        if not email:
            raise ValueError(f"Cannot resolve Slack user {slack_user_id} — no email found")

        user_id = deterministic_id("users", email)

        # Try to find or create via AuthService pattern
        from p8.services.auth import AuthService
        auth = AuthService(self._db, None, self._settings)  # type: ignore[arg-type]
        try:
            user, tenant_id = await auth._find_or_create_by_email(email)
            return user.id, tenant_id
        except Exception as e:
            logger.warning("Could not find/create user for %s: %s", email, e)
            # Fallback: return deterministic ID, use user_id as tenant
            return user_id, str(user_id)


# ---------------------------------------------------------------------------
# Logging handler — forwards ERROR+ to Slack alerts channel
# ---------------------------------------------------------------------------

class SlackAlertHandler(logging.Handler):
    """Logging handler that posts ERROR and CRITICAL messages to Slack.

    Attach to the root logger (or any logger) to get automatic Slack alerts
    for errors. Uses the SlackService.post_alert() method.

    Ignores errors originating from the slack service itself to avoid loops.
    """

    def __init__(self, slack_service: SlackService, level: int = logging.ERROR):
        super().__init__(level)
        self._slack = slack_service

    def emit(self, record: logging.LogRecord) -> None:
        # Avoid recursion: skip logs from slack_sdk or this module
        if record.name.startswith(("slack_sdk", "p8.services.slack")):
            return
        try:
            parts = [f"*{record.levelname}* in `{record.name}`"]
            parts.append(f"```{record.getMessage()[:1500]}```")
            if record.exc_info and record.exc_info[1]:
                tb = "".join(traceback.format_exception(*record.exc_info))
                parts.append(f"```{tb[-1000:]}```")
            self._slack.post_alert("\n".join(parts))
        except Exception:
            # Never let a logging handler crash the app
            pass


def setup_slack_logging(slack_service: SlackService) -> None:
    """Attach SlackAlertHandler to the root logger."""
    handler = SlackAlertHandler(slack_service)
    logging.getLogger().addHandler(handler)
