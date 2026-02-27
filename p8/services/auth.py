"""Auth service — tenant & user lifecycle, JWT tokens, OAuth 2.1 AS, magic link.

Dual role:
  1. **Identity provider** — tenant/user CRUD, Google/Apple OAuth callbacks,
     magic link passwordless flow.
  2. **OAuth 2.1 Authorization Server** — Dynamic Client Registration (RFC 7591),
     authorization codes with PKCE (S256), token exchange.  MCP clients
     (e.g. Claude Desktop) discover the AS via ``/.well-known/oauth-authorization-server``
     and authenticate through the same Google/Apple callback as the browser flow.

The MCP server (``mcp_server.py``) acts as a **resource server** — it validates
the HS256 JWTs issued here via ``RemoteAuthProvider`` + ``JWTVerifier`` but never
issues tokens itself.

OAuth 2.1 state is stored in the ``kv_store`` UNLOGGED table:
  - ``entity_type='oauth_client'`` — registered OAuth clients (DCR)
  - ``entity_type='auth_code'``    — authorization codes (single-use, PKCE)
  - ``entity_type='auth_token'``   — refresh token JTIs + magic link JTIs


  - Replaced FastMCP GoogleProvider with RemoteAuthProvider + JWTVerifier(HS256).
  - Added OAuth 2.1 AS methods: register_client, create/exchange_authorization_code.
  - Fixed asyncpg JSONB double-encoding: set_authorization_code_user now does
    read-merge-write as plain TEXT instead of atomic ``||`` with ``$1::jsonb``.
    See ``_parse_code_record()`` for the recovery logic.
  - Added ``"devices"`` to Repository._JSONB_COLUMNS so User.devices (stored as
    JSON string in Postgres) is parsed before Pydantic validation.

Testing Google OAuth
--------------------
1. Set P8_GOOGLE_CLIENT_ID and P8_GOOGLE_CLIENT_SECRET in .env
2. Add redirect URI in Google Cloud Console:
   http://localhost:8000/auth/callback/google
3. Start server: ``p8 serve``
4. Open in browser: ``http://localhost:8000/auth/authorize?provider=google``
5. Sign in with Google — redirects back to /auth/callback/google
6. Callback issues tokens as HttpOnly cookies and redirects to /

Testing MCP OAuth 2.1 (Claude Desktop)
---------------------------------------
1. MCP client connects → gets 401 → discovers AS via .well-known
2. Client registers via ``POST /auth/register``
3. Client opens ``/auth/authorize?client_id=...&code_challenge=...&state=...``
4. User authenticates with Google/Apple
5. Callback redirects to client's redirect_uri with ``?code=...&state=...``
6. Client exchanges code at ``POST /auth/token`` with PKCE verifier
7. Client uses Bearer JWT on MCP requests
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from uuid import UUID, uuid4

import jwt

from p8.ontology.types import Tenant, User
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository
from p8.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _parse_code_record(data: object) -> dict:
    """Parse an authorization code record from kv_store ``content_summary``.

    Handles two storage formats:

    1. **Normal dict** — returned as-is.  This is the expected format after
       ``set_authorization_code_user`` was fixed to use read-merge-write.
    2. **Array (legacy double-encoding)** — asyncpg's pool-level JSONB codec
       (``set_type_codec("jsonb", encoder=json.dumps)``) can double-encode
       string parameters when SQL uses ``$1::jsonb``.  PostgreSQL's ``||``
       then concatenates ``{original_object}`` and ``"double-encoded-string"``
       as a JSON array instead of merging objects.  We recover by iterating
       the array and merging all dict elements (parsing any JSON strings).

    The fix in ``set_authorization_code_user`` prevents new corruption, but
    this parser remains as a safety net for any codes created before the fix.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        merged: dict = {}
        for item in data:
            if isinstance(item, dict):
                merged.update(item)
            elif isinstance(item, str):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, dict):
                        merged.update(parsed)
                except (ValueError, TypeError):
                    pass
        return merged
    return {}


def _coerce_user_row(data: dict) -> None:
    """Parse JSONB columns that asyncpg may return as strings.

    The ``devices`` column is JSONB but can come back as a raw JSON string
    when read via direct SQL (outside Repository, which handles this in
    ``_decrypt_row``).  This caused a Pydantic validation error in production:
    ``Input should be a valid list [type=list_type, input_type=str]``.

    Mutates *data* in place.
    """
    if isinstance(data.get("devices"), str):
        data["devices"] = json.loads(data["devices"])


class AuthService:
    """Unified auth service — identity, OAuth 2.1 AS, JWT tokens, magic link.

    Injected into the FastAPI app as ``app.state.auth``.  All state is stored
    in PostgreSQL (users table + kv_store UNLOGGED table).  No in-memory session
    state is required — the service is stateless and horizontally scalable.
    """

    def __init__(
        self,
        db: Database,
        encryption: EncryptionService,
        settings: Settings | None = None,
    ):
        self.db = db
        self.encryption = encryption
        self.settings = settings or get_settings()
        self.tenants = Repository(Tenant, db, encryption)
        self.users = Repository(User, db, encryption)

    # -- kv_store helpers (DRY JSON read/write for OAuth state) ----------------

    async def _kv_get_json(self, key: str, entity_type: str) -> dict | None:
        """Read a JSON object from kv_store by key + entity_type."""
        row = await self.db.fetchrow(
            "SELECT content_summary FROM kv_store"
            " WHERE entity_key = $1 AND entity_type = $2",
            key, entity_type,
        )
        if not row:
            return None
        return dict(json.loads(row["content_summary"]))

    async def _kv_set_json(self, key: str, entity_type: str, data: dict) -> None:
        """Upsert a JSON object into kv_store (plain TEXT, avoids JSONB codec)."""
        await self.db.execute(
            "INSERT INTO kv_store (entity_key, entity_type, entity_id, content_summary)"
            " VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (COALESCE(tenant_id, ''), entity_key)"
            " DO UPDATE SET content_summary = $4",
            key, entity_type, uuid4(), json.dumps(data),
        )

    async def _kv_delete_returning_json(self, key: str, entity_type: str) -> dict | None:
        """Delete a kv_store row and return its parsed JSON content."""
        row = await self.db.fetchrow(
            "DELETE FROM kv_store WHERE entity_key = $1 AND entity_type = $2"
            " RETURNING content_summary",
            key, entity_type,
        )
        if not row:
            return None
        return _parse_code_record(json.loads(row["content_summary"]))

    # -----------------------------------------------------------------------
    # Tenant methods (unchanged)
    # -----------------------------------------------------------------------

    async def create_tenant(
        self,
        name: str,
        *,
        encryption_mode: str = "platform",
        own_key: bool = False,
    ) -> Tenant:
        """Create a new tenant and configure its encryption.

        encryption_mode: platform | client | sealed | disabled
        own_key: if True, generate a dedicated DEK for this tenant
        """
        tenant = Tenant(name=name, encryption_mode=encryption_mode)
        # Set tenant_id to self so scoped queries work
        tenant.tenant_id = str(tenant.id)
        [result] = await self.tenants.upsert(tenant)

        # Configure encryption based on mode
        if encryption_mode == "disabled":
            await self.encryption.configure_tenant(str(result.id), enabled=False)
        elif encryption_mode == "sealed":
            await self.encryption.configure_tenant_sealed(str(result.id))
        else:
            await self.encryption.configure_tenant(
                str(result.id), enabled=True, own_key=own_key, mode=encryption_mode
            )

        return result

    async def get_tenant(self, tenant_id: UUID) -> Tenant | None:
        return await self.tenants.get(tenant_id)

    async def configure_tenant_encryption(
        self,
        tenant_id: UUID,
        mode: str,
        *,
        public_key_pem: bytes | None = None,
    ) -> dict:
        """Reconfigure encryption for an existing tenant.

        Returns status dict with mode and any generated keys.
        """
        tenant = await self.tenants.get(tenant_id)
        if not tenant:
            return {"error": "tenant_not_found"}

        tid = str(tenant_id)
        result: dict = {"tenant_id": tid, "mode": mode}

        if mode == "disabled":
            await self.encryption.configure_tenant(tid, enabled=False)
        elif mode == "sealed":
            private_pem = await self.encryption.configure_tenant_sealed(
                tid, public_key_pem=public_key_pem
            )
            if private_pem:
                result["private_key_pem"] = private_pem.decode("ascii")
        else:
            await self.encryption.configure_tenant(
                tid, enabled=True, own_key=True, mode=mode
            )

        # Update tenant record
        tenant.encryption_mode = mode
        await self.tenants.upsert(tenant)

        return result

    # -----------------------------------------------------------------------
    # User methods (unchanged)
    # -----------------------------------------------------------------------

    async def create_user(
        self,
        name: str,
        email: str,
        *,
        tenant_id: str,
        provider: str | None = None,
        provider_user_id: str | None = None,
    ) -> User:
        """Create a user under an existing tenant.

        Provider info (google, apple) stored in metadata JSONB.
        """
        user = User(name=name, email=email, tenant_id=tenant_id)

        if provider:
            user.metadata = {
                "auth_provider": provider,
                "provider_user_id": provider_user_id,
            }

        [result] = await self.users.upsert(user)
        return result

    async def get_user(self, user_id: UUID, *, tenant_id: str) -> User | None:
        return await self.users.get_for_tenant(user_id, tenant_id=tenant_id)

    async def get_user_by_email(self, email: str, *, tenant_id: str) -> User | None:
        """Look up user by email using deterministic encryption for exact match.

        Email is stored with deterministic encryption, so we encrypt the
        search value with the same key and match the ciphertext.
        """
        # Ensure DEK is cached so encrypt_fields works (sync method)
        await self.encryption.get_dek(tenant_id)

        # Encrypt the lookup email deterministically
        search_data = {"email": email, "id": ""}
        encrypted = self.encryption.encrypt_fields(User, search_data, tenant_id)
        encrypted_email = encrypted.get("email", email)

        row = await self.db.fetchrow(
            "SELECT * FROM users WHERE email = $1 AND tenant_id = $2 AND deleted_at IS NULL",
            encrypted_email,
            tenant_id,
        )
        if not row:
            return None

        should_decrypt = await self.encryption.should_decrypt_on_read(tenant_id)
        data = dict(row)
        if should_decrypt:
            data = self.encryption.decrypt_fields(User, data, tenant_id)
        return User.model_validate(data)

    async def find_users(self, *, tenant_id: str, limit: int = 50) -> list[User]:
        return await self.users.find_for_tenant(tenant_id=tenant_id, limit=limit)

    # -----------------------------------------------------------------------
    # Convenience
    # -----------------------------------------------------------------------

    async def create_personal_tenant(
        self,
        name: str,
        email: str,
        *,
        encryption_mode: str = "platform",
    ) -> tuple[Tenant, User]:
        """Create a 1:1 personal tenant + user in one call.

        Tenant name = user name. Returns (tenant, user).
        """
        tenant = await self.create_tenant(name, encryption_mode=encryption_mode, own_key=True)
        user = await self.create_user(name, email, tenant_id=str(tenant.id))
        return tenant, user

    # -----------------------------------------------------------------------
    # JWT token operations
    # -----------------------------------------------------------------------

    def create_access_token(self, user: User, tenant_id: str) -> str:
        """Create a short-lived HS256 access token."""
        now = int(time.time())
        payload = {
            "sub": str(user.id),
            "email": user.email,
            "tenant_id": tenant_id,
            "provider": (user.metadata or {}).get("auth_provider", "magic_link"),
            "scopes": [],
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + self.settings.auth_access_token_expiry,
            "type": "access",
        }
        return jwt.encode(payload, self.settings.auth_secret_key, algorithm="HS256")

    def create_refresh_token(self, user: User, tenant_id: str) -> tuple[str, str]:
        """Create a long-lived refresh token. Returns (token, jti)."""
        now = int(time.time())
        jti = str(uuid4())
        payload = {
            "sub": str(user.id),
            "tenant_id": tenant_id,
            "jti": jti,
            "iat": now,
            "exp": now + self.settings.auth_refresh_token_expiry,
            "type": "refresh",
        }
        token = jwt.encode(payload, self.settings.auth_secret_key, algorithm="HS256")
        return token, jti

    def verify_token(self, token: str) -> dict:
        """Decode and validate a JWT. Raises jwt.PyJWTError on failure."""
        return jwt.decode(token, self.settings.auth_secret_key, algorithms=["HS256"])

    async def issue_tokens(self, user: User, tenant_id: str) -> dict:
        """Issue an access + refresh token pair. Stores refresh jti in kv_store."""
        access_token = self.create_access_token(user, tenant_id)
        refresh_token, jti = self.create_refresh_token(user, tenant_id)
        await self.store_refresh_jti(jti, user.id)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": self.settings.auth_access_token_expiry,
        }

    async def refresh_tokens(self, refresh_token: str) -> dict:
        """Rotate refresh token: verify old, revoke, issue new pair."""
        payload = self.verify_token(refresh_token)
        if payload.get("type") != "refresh":
            raise jwt.InvalidTokenError("not a refresh token")

        jti = payload["jti"]
        if not await self.is_refresh_valid(jti):
            raise jwt.InvalidTokenError("refresh token revoked or consumed")

        # Revoke old
        await self.revoke_refresh_jti(jti)

        # Find user and issue new pair
        user_id = UUID(payload["sub"])
        tenant_id = payload["tenant_id"]
        user = await self.get_user(user_id, tenant_id=tenant_id)
        if not user:
            raise jwt.InvalidTokenError("user not found")

        return await self.issue_tokens(user, tenant_id)

    async def store_refresh_jti(self, jti: str, user_id: UUID) -> None:
        """Track a refresh token jti in kv_store."""
        await self.db.execute(
            "INSERT INTO kv_store (entity_key, entity_type, entity_id)"
            " VALUES ($1, $2, $3)"
            " ON CONFLICT (COALESCE(tenant_id, ''), entity_key) DO UPDATE SET entity_id = $3",
            f"refresh:{jti}",
            "auth_token",
            user_id,
        )

    async def revoke_refresh_jti(self, jti: str) -> None:
        """Remove a refresh token jti from kv_store (single-use)."""
        await self.db.execute("DELETE FROM kv_store WHERE entity_key = $1", f"refresh:{jti}")

    async def is_refresh_valid(self, jti: str) -> bool:
        """Check if a refresh token jti exists in kv_store."""
        row = await self.db.fetchrow(
            "SELECT 1 FROM kv_store WHERE entity_key = $1", f"refresh:{jti}"
        )
        return row is not None

    # -----------------------------------------------------------------------
    # OAuth callbacks
    # -----------------------------------------------------------------------

    async def find_user_by_provider(
        self, provider: str, provider_user_id: str
    ) -> tuple[User, str] | None:
        """Find a user by auth provider + provider_user_id across all tenants.

        Uses JSONB metadata query: ``metadata->>'auth_provider'``.
        Handles devices stored as JSON string (see _coerce_user_row).
        """
        row = await self.db.fetchrow(
            "SELECT * FROM users WHERE metadata->>'auth_provider' = $1"
            " AND metadata->>'provider_user_id' = $2 AND deleted_at IS NULL",
            provider,
            provider_user_id,
        )
        if not row:
            return None
        data = dict(row)
        tenant_id = data.get("tenant_id", "")
        should_decrypt = await self.encryption.should_decrypt_on_read(tenant_id)
        if should_decrypt:
            data = self.encryption.decrypt_fields(User, data, tenant_id)
        _coerce_user_row(data)
        return User.model_validate(data), tenant_id

    async def handle_google_callback(
        self, user_info: dict
    ) -> tuple[User, str]:
        """Find-or-create user from Google OIDC user_info.

        user_info keys: sub, email, name, picture, email_verified
        """
        sub = user_info["sub"]
        existing = await self.find_user_by_provider("google", sub)
        if existing:
            user, tenant_id = existing
            # Update picture on every login (Google URLs rotate)
            picture = user_info.get("picture")
            if picture and (user.metadata or {}).get("picture") != picture:
                user.metadata = {**(user.metadata or {}), "picture": picture}
                [user] = await self.users.upsert(user)
            return user, tenant_id

        # New user — create personal tenant
        name = user_info.get("name", user_info.get("email", "Google User"))
        email = user_info["email"]
        tenant, user = await self.create_personal_tenant(name, email)
        # Update with provider metadata
        user.metadata = {
            "auth_provider": "google",
            "provider_user_id": sub,
            "picture": user_info.get("picture"),
        }
        [user] = await self.users.upsert(user)
        return user, str(tenant.id)

    async def handle_apple_callback(
        self, token_data: dict, user_info: dict | None = None
    ) -> tuple[User, str]:
        """Find-or-create user from Apple Sign In.

        token_data: decoded id_token claims (sub, email)
        user_info: optional name dict from first auth only (Apple sends it once)
        """
        sub = token_data["sub"]
        existing = await self.find_user_by_provider("apple", sub)
        if existing:
            return existing

        # New user — Apple may provide name only on first auth
        email = token_data.get("email", "")
        name = "Apple User"
        if user_info:
            first = user_info.get("firstName", "")
            last = user_info.get("lastName", "")
            name = f"{first} {last}".strip() or name

        tenant, user = await self.create_personal_tenant(name, email)
        user.metadata = {
            "auth_provider": "apple",
            "provider_user_id": sub,
        }
        [user] = await self.users.upsert(user)
        return user, str(tenant.id)

    def generate_apple_client_secret(self) -> str:
        """Generate ES256 JWT client_secret for Apple Sign In.

        Apple requires a dynamic client_secret signed with the .p8 private key.
        """
        s = self.settings
        now = int(time.time())
        headers = {"kid": s.apple_key_id}
        payload = {
            "iss": s.apple_team_id,
            "iat": now,
            "exp": now + 86400 * 180,  # max 6 months
            "aud": "https://appleid.apple.com",
            "sub": s.apple_client_id,
        }
        with open(s.apple_private_key_path) as f:
            private_key = f.read()
        return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)

    # -----------------------------------------------------------------------
    # OAuth 2.1 Authorization Server — Dynamic Client Registration + Auth Codes
    # -----------------------------------------------------------------------

    async def register_client(self, metadata: dict) -> dict:
        """Register an OAuth client (RFC 7591 Dynamic Client Registration).

        Stores client in kv_store with ``entity_type='oauth_client'``.
        Returns client_id, client_secret, and echoed metadata.

        MCP clients (e.g. Claude Desktop) call this automatically during
        the discovery flow before initiating authorization.
        """
        client_id = str(uuid4())
        client_secret = str(uuid4())

        client_record = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": metadata.get("redirect_uris", []),
            "client_name": metadata.get("client_name", ""),
            "grant_types": metadata.get("grant_types", ["authorization_code"]),
            "response_types": metadata.get("response_types", ["code"]),
            "token_endpoint_auth_method": metadata.get("token_endpoint_auth_method", "client_secret_post"),
        }

        await self._kv_set_json(f"oauth_client:{client_id}", "oauth_client", client_record)
        return client_record

    async def get_client(self, client_id: str) -> dict | None:
        """Retrieve a registered OAuth client by client_id."""
        return await self._kv_get_json(f"oauth_client:{client_id}", "oauth_client")

    def authenticate_client(self, client_id: str, client_secret: str, client_record: dict) -> bool:
        """Verify client credentials (constant-time comparison)."""
        return hmac.compare_digest(client_record.get("client_secret", ""), client_secret)

    async def create_authorization_code(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scope: str,
        provider: str,
        provider_state: str | None = None,
    ) -> str:
        """Generate and store an authorization code with PKCE challenge.

        Stored in kv_store with ``entity_type='auth_code'``.  The code is
        created *before* the user authenticates — ``set_authorization_code_user``
        attaches the user identity after the OAuth callback completes.

        ``provider_state`` is the MCP client's ``state`` param, forwarded
        back on the redirect so the client can correlate the response.
        """
        code = str(uuid4())
        code_record = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scope": scope,
            "provider": provider,
            "client_state": provider_state,
        }

        await self._kv_set_json(f"auth_code:{code}", "auth_code", code_record)
        return code

    async def get_authorization_code(self, code: str) -> dict | None:
        """Retrieve an authorization code record (does not consume it).

        Uses ``_parse_code_record`` for resilience against the JSONB
        double-encoding bug (see module docstring).
        """
        row = await self.db.fetchrow(
            "SELECT content_summary FROM kv_store"
            " WHERE entity_key = $1 AND entity_type = 'auth_code'",
            f"auth_code:{code}",
        )
        if not row:
            return None
        return _parse_code_record(json.loads(row["content_summary"]))

    async def consume_authorization_code(self, code: str) -> dict | None:
        """Atomically retrieve and delete an authorization code (single-use).

        The DELETE … RETURNING guarantees the code can only be exchanged once.
        """
        return await self._kv_delete_returning_json(f"auth_code:{code}", "auth_code")

    async def set_authorization_code_user(
        self, code: str, user_id: str, tenant_id: str, email: str | None = None,
    ) -> None:
        """Attach user info to an existing authorization code after OAuth callback.

        Uses **read-merge-write as plain TEXT** to avoid asyncpg's JSONB codec
        double-encoding the parameter.  The original implementation used an
        atomic ``content_summary || $1::jsonb`` UPDATE, but asyncpg's pool-level
        JSONB encoder (``set_type_codec("jsonb", encoder=json.dumps)``) applied
        ``json.dumps`` a second time, turning the patch dict into a JSONB scalar
        string.  PostgreSQL's ``||`` then produced an array
        ``[{original}, "escaped-string"]`` instead of a merged object.

        See ``_parse_code_record()`` and ``tests/unit/test_auth_codes.py``.
        """
        row = await self.db.fetchrow(
            "SELECT content_summary FROM kv_store"
            " WHERE entity_key = $1 AND entity_type = 'auth_code'",
            f"auth_code:{code}",
        )
        if not row:
            logger.warning("set_authorization_code_user: code=%s not found", code[:12])
            return
        record = _parse_code_record(json.loads(row["content_summary"]))
        record.update({"user_id": user_id, "tenant_id": tenant_id, "email": email})
        await self.db.execute(
            "UPDATE kv_store SET content_summary = $1"
            " WHERE entity_key = $2 AND entity_type = 'auth_code'",
            json.dumps(record), f"auth_code:{code}",
        )

    async def exchange_authorization_code(
        self,
        code: str,
        client_id: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict:
        """Exchange an authorization code for access + refresh tokens.

        Validates (in order):
          1. Code exists and is consumed (single-use)
          2. ``client_id`` matches the code's registered client
          3. ``redirect_uri`` matches the code's registered redirect
          4. PKCE: ``SHA256(code_verifier) == stored code_challenge`` (S256)

        Then looks up the user (attached by ``set_authorization_code_user``
        during the OAuth callback) and issues an HS256 JWT token pair.

        Falls back to a direct DB query if the user lookup via Repository
        fails (e.g. tenant_id missing), and to a client-only JWT if no
        user is found at all (so the MCP flow still completes).

        Raises ``ValueError`` on any validation failure.
        """
        record = await self.consume_authorization_code(code)
        if not record:
            raise ValueError("Invalid or expired authorization code")

        if record["client_id"] != client_id:
            raise ValueError("client_id mismatch")
        if record["redirect_uri"] != redirect_uri:
            raise ValueError("redirect_uri mismatch")

        # PKCE S256 verification
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if computed_challenge != record["code_challenge"]:
            raise ValueError("PKCE verification failed")

        # Look up user — set by callback via set_authorization_code_user
        user_id = record.get("user_id")
        tenant_id = record.get("tenant_id") or ""

        user = None
        if user_id and tenant_id:
            user = await self.get_user(UUID(user_id), tenant_id=tenant_id)
        if not user and user_id:
            # Fallback: direct query without tenant scoping (handles edge case
            # where tenant_id was lost during session)
            row = await self.db.fetchrow(
                "SELECT * FROM users WHERE id = $1 AND deleted_at IS NULL",
                UUID(user_id),
            )
            if row:
                data = dict(row)
                tenant_id = str(data.get("tenant_id", ""))
                _coerce_user_row(data)
                user = User.model_validate(data)

        if user and tenant_id:
            return await self.issue_tokens(user, tenant_id)

        # Last resort: issue a minimal JWT with client_id as subject
        now = int(time.time())
        payload = {
            "sub": client_id,
            "client_id": client_id,
            "scope": record.get("scope", "openid"),
            "type": "access",
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + 3600,
        }
        return {
            "access_token": jwt.encode(payload, self.settings.auth_secret_key, algorithm="HS256"),
            "token_type": "bearer",
            "expires_in": 3600,
        }

    # -----------------------------------------------------------------------
    # Magic link
    # -----------------------------------------------------------------------

    async def create_magic_link_token(self, email: str) -> str:
        """Create a signed single-use magic link JWT. Stores jti in kv_store."""
        jti = str(uuid4())
        now = int(time.time())
        payload = {
            "email": email,
            "jti": jti,
            "iat": now,
            "exp": now + self.settings.auth_magic_link_expiry,
            "type": "magic_link",
        }
        token = jwt.encode(payload, self.settings.auth_secret_key, algorithm="HS256")

        # Store jti for single-use verification (entity_id is a placeholder UUID)
        await self.db.execute(
            "INSERT INTO kv_store (entity_key, entity_type, entity_id, content_summary)"
            " VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (COALESCE(tenant_id, ''), entity_key)"
            " DO UPDATE SET content_summary = $4",
            f"magic:{jti}",
            "auth_token",
            uuid4(),
            email,
        )
        return token

    async def verify_magic_link(self, token: str) -> tuple[User, str]:
        """Verify magic link token, consume it, find-or-create user.

        Returns (user, tenant_id). Raises jwt.PyJWTError on invalid/expired token.
        """
        payload = self.verify_token(token)
        if payload.get("type") != "magic_link":
            raise jwt.InvalidTokenError("not a magic link token")

        jti = payload["jti"]
        # Check single-use
        row = await self.db.fetchrow(
            "SELECT 1 FROM kv_store WHERE entity_key = $1", f"magic:{jti}"
        )
        if not row:
            raise jwt.InvalidTokenError("magic link already used")

        # Consume the token
        await self.db.execute("DELETE FROM kv_store WHERE entity_key = $1", f"magic:{jti}")

        email = payload["email"]
        return await self._find_or_create_by_email(email)

    async def _find_or_create_by_email(self, email: str) -> tuple[User, str]:
        """Find user by email across tenants, or create a new personal tenant."""
        # Search across all tenants for this email
        row = await self._find_user_row_by_email(email)
        if row:
            data = dict(row)
            tenant_id = data.get("tenant_id", "")
            should_decrypt = await self.encryption.should_decrypt_on_read(tenant_id)
            if should_decrypt:
                data = self.encryption.decrypt_fields(User, data, tenant_id)
            return User.model_validate(data), tenant_id

        # New user
        name = email.split("@")[0]
        tenant, user = await self.create_personal_tenant(name, email)
        return user, str(tenant.id)

    async def _find_user_row_by_email(self, email: str):
        """Find user row by email, trying deterministic encryption per tenant."""
        # First try plaintext match (for tenants with encryption disabled)
        row = await self.db.fetchrow(
            "SELECT * FROM users WHERE email = $1 AND deleted_at IS NULL", email
        )
        if row:
            return row

        # Try all active tenants with encryption
        tenant_rows = await self.db.fetch(
            "SELECT id FROM tenants WHERE status = 'active' AND deleted_at IS NULL"
        )
        for trow in tenant_rows:
            tid = str(trow["id"])
            try:
                await self.encryption.get_dek(tid)
                search_data = {"email": email, "id": ""}
                encrypted = self.encryption.encrypt_fields(User, search_data, tid)
                encrypted_email = encrypted.get("email", email)
                row = await self.db.fetchrow(
                    "SELECT * FROM users WHERE email = $1 AND tenant_id = $2"
                    " AND deleted_at IS NULL",
                    encrypted_email,
                    tid,
                )
                if row:
                    return row
            except Exception:
                continue
        return None

    async def send_magic_link(self, email: str) -> None:
        """Generate a magic link token and send it via the configured email provider."""
        token = await self.create_magic_link_token(email)
        base_url = self.settings.magic_link_base_url or self.settings.api_base_url
        link = f"{base_url}/auth/verify?token={token}"

        provider = self.settings.email_provider
        if provider == "console":
            logger.info("Magic link for %s: %s", email, link)
            print(f"\n🔗 Magic link for {email}:\n   {link}\n")  # noqa: T201
        elif provider == "smtp":
            await self._send_smtp(email, link)
        elif provider == "resend":
            await self._send_resend(email, link)

    async def _send_smtp(self, to: str, link: str) -> None:
        """Send magic link via SMTP."""
        import smtplib
        from email.message import EmailMessage

        s = self.settings
        msg = EmailMessage()
        msg["Subject"] = "Sign in to p8"
        msg["From"] = s.email_from
        msg["To"] = to
        msg.set_content(f"Click to sign in:\n\n{link}\n\nThis link expires in 10 minutes.")

        with smtplib.SMTP(s.smtp_host, s.smtp_port) as server:
            server.starttls()
            if s.smtp_username:
                server.login(s.smtp_username, s.smtp_password)
            server.send_message(msg)

    async def _send_resend(self, to: str, link: str) -> None:
        """Send magic link via Resend API."""
        import httpx

        s = self.settings
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {s.resend_api_key}"},
                json={
                    "from": s.email_from,
                    "to": [to],
                    "subject": "Sign in to p8",
                    "text": f"Click to sign in:\n\n{link}\n\nThis link expires in 10 minutes.",
                },
            )
