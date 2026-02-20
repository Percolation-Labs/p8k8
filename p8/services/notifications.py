"""Push notification relay — APNs (iOS) + FCM (Android).

===============================================================================
CONFIGURATION GUIDE
===============================================================================


1. OVERVIEW — HOW IT ALL FITS TOGETHER
---------------------------------------

Device tokens live on the User record in a `devices` JSONB array. No
separate tables. The flow is:

    Mobile app                      p8 API                    Apple/Google
    ──────────                      ──────                    ────────────
    1. Login (OAuth/magic link) ──→ issue JWT
    2. Request push permission
       from OS (APNs/FCM SDK)
    3. OS returns device token
    4. PATCH /auth/me ────────────→ save token to user.devices
       {"devices": [...]}
                                              ...later...
    5.                              pg_cron fires ──→ POST /notifications/send
                                    reads user.devices
                                    ──→ POST to APNs HTTP/2 ──→ Apple
                                    ──→ POST to FCM v1 ───────→ Google
    6. Push arrives on device

The service auto-deactivates tokens when Apple returns 410 (Gone) or
Google returns UNREGISTERED — sets {"active": false} on that device entry
so future sends skip it.


2. APPLE APNs SETUP (iOS)
--------------------------

APNs reuses the same .p8 key you already have for Sign In with Apple.
If you set up Apple Sign In, you already have everything — just add the
bundle ID and make sure the key has push entitlements.

Step-by-step:

  a) Apple Developer portal → Certificates, Identifiers & Profiles → Keys
     - You should already have a key for Sign In with Apple
     - Click on it → confirm "Apple Push Notifications service (APNs)" is
       checked. If not, enable it and download the new .p8 file.
     - Note the Key ID (10-char alphanumeric, e.g. ABC123DEFG)

  b) Note your Team ID — visible top-right of the portal, or in
     Membership → Team ID (10-char, e.g. TEAM123456)

  c) Note your app's Bundle ID — in Identifiers → App IDs, e.g.
     com.percolationlabs.p8

  d) Add to .env:

       # Shared with Apple Sign In (you likely have these already):
       P8_APPLE_KEY_ID=ABC123DEFG
       P8_APPLE_TEAM_ID=TEAM123456
       P8_APPLE_PRIVATE_KEY_PATH=./AuthKey_ABC123DEFG.p8

       # New for push notifications:
       P8_APNS_BUNDLE_ID=com.percolationlabs.p8
       P8_APNS_ENVIRONMENT=production

  e) That's it. The service constructs an ES256 "provider token" JWT
     signed with the .p8 key and sends it in the authorization header
     of every APNs request. The JWT is cached for 58 minutes (Apple
     allows up to 60).

Environment notes:
  - Use "sandbox" during development / TestFlight
  - Use "production" for App Store builds
  - The endpoints are different:
      sandbox:    https://api.sandbox.push.apple.com/3/device/{token}
      production: https://api.push.apple.com/3/device/{token}

APNs requires HTTP/2 — that's why pyproject.toml specifies httpx[http2].
The h2 library is pulled in automatically.

APNs payload format sent by this service:

    {
        "aps": {
            "alert": {"title": "...", "body": "..."},
            "sound": "default"
        },
        ...extra data keys merged at top level...
    }

Common APNs error codes:
  - 200         → delivered successfully
  - 400         → bad request (malformed payload, missing headers)
  - 403         → wrong key/team/bundle combination
  - 410         → device token is no longer valid (auto-deactivated)
  - 429         → too many requests to this device


3. GOOGLE FCM v1 SETUP (Android)
---------------------------------

FCM v1 uses a Google service account for OAuth2 authentication. This is
separate from any Google OAuth client you have for Sign In with Google.

Step-by-step:

  a) Firebase Console (https://console.firebase.google.com)
     - Select your project (or create one)
     - Project Settings → General → note "Project ID" (e.g. my-app-12345)

  b) Project Settings → Service accounts
     - Click "Generate new private key"
     - This downloads a JSON file like:

         {
           "type": "service_account",
           "project_id": "my-app-12345",
           "private_key_id": "...",
           "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...",
           "client_email": "firebase-adminsdk-xxxxx@my-app-12345.iam.gserviceaccount.com",
           "client_id": "...",
           ...
         }

     - Save this file securely (e.g. ./firebase-sa.json)
     - NEVER commit it to git — add to .gitignore

  c) Enable the FCM API:
     - Google Cloud Console → APIs & Services → Library
     - Search "Firebase Cloud Messaging API" (the v1 one, NOT legacy)
     - Click Enable

  d) Add to .env:

       P8_FCM_PROJECT_ID=my-app-12345
       P8_FCM_SERVICE_ACCOUNT_FILE=./firebase-sa.json

  e) Done. The service loads the JSON once, obtains an OAuth2 access
     token scoped to firebase.messaging, and auto-refreshes it when
     expired (via google-auth library).

FCM v1 message format sent by this service:

    POST https://fcm.googleapis.com/v1/projects/{project_id}/messages:send

    {
        "message": {
            "token": "device-registration-token",
            "notification": {"title": "...", "body": "..."},
            "data": {"key": "value"}   ← optional, values must be strings
        }
    }

Common FCM error codes:
  - 200             → delivered to FCM (not necessarily to device)
  - 400             → invalid argument (bad token format, missing fields)
  - 401             → OAuth token invalid or expired (auto-retried)
  - 404             → UNREGISTERED — token no longer valid (auto-deactivated)
  - 429             → quota exceeded (FCM has per-project rate limits)


4. DEVICE REGISTRATION — MOBILE APP FLOW
------------------------------------------

After the user logs in and the OS grants a push token, the app registers
it with p8 via PATCH /auth/me. The `devices` field is a JSON array:

    PATCH /auth/me
    Authorization: Bearer <user-jwt>
    Content-Type: application/json

    {
        "devices": [
            {
                "platform": "apns",
                "token": "a1b2c3d4e5f6...hex-encoded-64-bytes",
                "device_name": "Cia's iPhone 15 Pro",
                "bundle_id": "com.percolationlabs.p8",
                "app_version": "1.2.0",
                "active": true
            }
        ]
    }

Device dict fields:

    platform     — "apns" or "fcm" (required)
    token        — the raw token string from the OS push SDK (required)
                   APNs: 64-char hex string from didRegisterForRemoteNotifications
                   FCM:  ~150-char string from FirebaseMessaging.getInstance().token
    device_name  — human label, e.g. "iPhone 15 Pro" (optional)
    bundle_id    — iOS bundle ID, useful if you have multiple apps (optional)
    app_version  — for filtering sends to specific versions (optional)
    active       — defaults to true; set false to pause notifications (optional)

Multiple devices per user are supported (e.g. iPhone + iPad + Android).
The app should send ALL its active tokens in each PATCH — this is a
full replacement of the devices array, not a merge. To add a second
device, read current devices first and append.

iOS (Swift) example:

    func application(_ app: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        // PATCH /auth/me with {"devices": [{"platform": "apns", "token": token, ...}]}
    }

Android (Kotlin) example:

    FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
        // PATCH /auth/me with {"devices": [{"platform": "fcm", "token": token, ...}]}
    }


5. SENDING NOTIFICATIONS — API
--------------------------------

Send to one or more users:

    POST /notifications/send
    Authorization: Bearer <api-key-or-jwt>
    Content-Type: application/json

    {
        "user_ids": ["550e8400-e29b-41d4-a716-446655440000"],
        "title": "New message",
        "body": "You have a new message from Alice",
        "data": {"screen": "chat", "session_id": "abc123"}
    }

The `data` dict is delivered as the notification payload — your app can
read it to deep-link or navigate on tap. For APNs, data keys are merged
into the top-level payload alongside "aps". For FCM, they go into the
"data" field (values are stringified automatically).

Response:

    {
        "results": [
            {"token": "a1b2...", "platform": "apns", "status": "delivered", "apns_id": "..."},
            {"token": "x9y8...", "platform": "fcm", "status": "delivered", "fcm_message_id": "..."},
            {"token": "dead...", "platform": "apns", "status": "error", "deactivated": true, ...}
        ]
    }


6. pg_cron + pg_net — SCHEDULED NOTIFICATIONS FROM POSTGRES
-------------------------------------------------------------

pg_cron and pg_net are Postgres extensions that let you schedule HTTP
calls directly from SQL. This is how you trigger notifications without
an external scheduler — the database IS the scheduler.

Prerequisites:
  - pg_cron and pg_net extensions installed (CloudNativePG supports both)
  - The API must be reachable from the Postgres pod
  - An API key set so pg_net can authenticate

a) Install extensions (add to install.sql or run manually):

    CREATE EXTENSION IF NOT EXISTS pg_cron;
    CREATE EXTENSION IF NOT EXISTS pg_net;

b) Store the API key as a Postgres GUC so SQL jobs can reference it:

    ALTER DATABASE p8 SET p8.api_key = 'your-P8_API_KEY-value';

   (Persists across restarts. The job reads it via current_setting().)

c) Example: Daily digest at 9 AM UTC to all opted-in users

    SELECT cron.schedule('daily-digest', '0 9 * * *', $$
        SELECT net.http_post(
            url := 'http://p8-api.p8.svc:8000/notifications/send',
            headers := jsonb_build_object(
                'Authorization', 'Bearer ' || current_setting('p8.api_key'),
                'Content-Type', 'application/json'
            ),
            body := jsonb_build_object(
                'user_ids', (
                    SELECT jsonb_agg(id::text)
                    FROM users
                    WHERE deleted_at IS NULL
                      AND metadata->>'digest_enabled' = 'true'
                      AND devices != '[]'::jsonb
                ),
                'title', 'Your Daily Digest',
                'body', 'Here is what happened yesterday...'
            )
        );
    $$);

d) Example: Reminder 5 minutes after a moment is created (one-shot)

    -- Call this from a trigger or application code:
    SELECT cron.schedule(
        'reminder-' || NEW.id::text,
        (EXTRACT(EPOCH FROM NEW.starts_timestamp + interval '5 minutes'))::text,
        format($$
            SELECT net.http_post(
                url := 'http://p8-api.p8.svc:8000/notifications/send',
                headers := '{"Authorization": "Bearer %s", "Content-Type": "application/json"}'::jsonb,
                body := '{"user_ids": ["%s"], "title": "Reminder", "body": "%s"}'::jsonb
            );
            SELECT cron.unschedule('reminder-%s');
        $$, current_setting('p8.api_key'), NEW.user_id, NEW.name, NEW.id)
    );

e) Manage jobs:

    SELECT * FROM cron.job;                    -- list all scheduled jobs
    SELECT cron.unschedule('daily-digest');     -- remove a job
    SELECT * FROM cron.job_run_details          -- check execution history
        ORDER BY start_time DESC LIMIT 10;

f) URL for the API inside the cluster:
   - Same namespace: http://p8-api:8000/notifications/send
   - Cross-namespace: http://p8-api.p8.svc:8000/notifications/send
   - From outside: https://your-domain.com/notifications/send

g) Cron schedule syntax (standard 5-field):
   ┌───────── minute (0-59)
   │ ┌─────── hour (0-23)
   │ │ ┌───── day of month (1-31)
   │ │ │ ┌─── month (1-12)
   │ │ │ │ ┌─ day of week (0-6, 0=Sunday)
   │ │ │ │ │
   * * * * *

   '0 9 * * *'      → every day at 09:00 UTC
   '*/5 * * * *'    → every 5 minutes
   '0 9 * * 1'      → every Monday at 09:00
   '0 0 1 * *'      → first day of each month at midnight


7. TESTING WITHOUT REAL CREDENTIALS
-------------------------------------

If APNs/FCM credentials are not configured, the service returns
{"status": "skipped", "error": "APNs not configured"} per device —
it won't crash. This lets you test the full registration → send flow
locally.

    # Start with push env vars unset
    p8 serve

    # Register a fake device token
    curl -X PATCH http://localhost:8000/auth/me \\
      -H "Authorization: Bearer $JWT" \\
      -H "Content-Type: application/json" \\
      -d '{"devices": [{"platform": "apns", "token": "fake-token-123"}]}'

    # Send (will return "skipped" per device, but exercises the full path)
    curl -X POST http://localhost:8000/notifications/send \\
      -H "Authorization: Bearer $P8_API_KEY" \\
      -H "Content-Type: application/json" \\
      -d '{"user_ids": ["<user-uuid>"], "title": "Test", "body": "Hello"}'

    # Check the user's devices field
    curl http://localhost:8000/auth/me -H "Authorization: Bearer $JWT"


8. ENVIRONMENT VARIABLE SUMMARY
---------------------------------

    # APNs (reuses Apple Sign In credentials):
    P8_APPLE_KEY_ID=ABC123DEFG                 # from Apple Developer → Keys
    P8_APPLE_TEAM_ID=TEAM123456                # from Apple Developer → Membership
    P8_APPLE_PRIVATE_KEY_PATH=./AuthKey.p8     # downloaded .p8 key file
    P8_APNS_BUNDLE_ID=com.percolationlabs.p8   # your iOS app bundle ID
    P8_APNS_ENVIRONMENT=production             # "production" or "sandbox"

    # FCM (separate service account):
    P8_FCM_PROJECT_ID=my-firebase-project      # from Firebase Console
    P8_FCM_SERVICE_ACCOUNT_FILE=./sa.json      # downloaded service account JSON

    # API key (needed for pg_cron to call /notifications/send):
    P8_API_KEY=your-secret-api-key

Both platforms are optional and independent — you can configure just APNs,
just FCM, or both. The service only attempts delivery for configured
platforms. If neither is set, NotificationService is not initialized at
all (the lifespan gate checks apns_bundle_id or fcm_project_id).

===============================================================================
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

import httpx

from p8.services.database import Database
from p8.settings import Settings

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

        Reads devices from user.devices JSONB. Returns result per device.
        Auto-deactivates tokens that return 410/UNREGISTERED.
        """
        row = await self._db.fetchrow(
            "SELECT devices FROM users WHERE id = $1 AND deleted_at IS NULL",
            user_id,
        )
        if not row or not row["devices"]:
            return []

        import json
        devices = json.loads(row["devices"]) if isinstance(row["devices"], str) else row["devices"]

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
        return creds.token

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
        # Use jsonb_agg to rebuild the devices array with matching tokens deactivated
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
