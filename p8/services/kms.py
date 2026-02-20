"""Pluggable KMS backends for DEK wrap/unwrap."""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KMSProvider(ABC):
    @abstractmethod
    async def wrap_and_store_dek(self, tenant_id: str, dek: bytes, *, mode: str = "platform") -> None: ...

    @abstractmethod
    async def unwrap_dek(self, tenant_id: str) -> bytes | None: ...

    @abstractmethod
    async def is_disabled(self, tenant_id: str) -> bool: ...

    @abstractmethod
    async def set_disabled(self, tenant_id: str) -> None: ...

    @abstractmethod
    async def remove_key(self, tenant_id: str) -> None: ...

    @abstractmethod
    async def get_mode(self, tenant_id: str) -> str | None: ...

    @abstractmethod
    async def set_mode(self, tenant_id: str, mode: str) -> None: ...

    @abstractmethod
    async def store_sealed_key(self, tenant_id: str, public_key_pem: bytes, *, origin: str = "server") -> None: ...

    @abstractmethod
    async def get_sealed_public_key(self, tenant_id: str) -> bytes | None: ...


# ---------------------------------------------------------------------------
# SQL helpers shared by both providers (sealed key ops hit the same table)
# ---------------------------------------------------------------------------

_SEALED_UPSERT = """INSERT INTO tenant_keys (tenant_id, wrapped_dek, kms_key_id, algorithm, status, mode)
   VALUES ($1, $2, $3, 'RSA-OAEP-SHA256', 'active', 'sealed')
   ON CONFLICT (tenant_id)
   DO UPDATE SET wrapped_dek = $2, kms_key_id = $3, algorithm = 'RSA-OAEP-SHA256',
                 status = 'active', mode = 'sealed', rotated_at = CURRENT_TIMESTAMP"""

_SEALED_SELECT = (
    "SELECT wrapped_dek FROM tenant_keys"
    " WHERE tenant_id = $1 AND mode = 'sealed' AND status = 'active'"
)


class LocalFileKMS(KMSProvider):
    """Dev KMS — master key in a local file, DEKs in tenant_keys table."""

    def __init__(self, keyfile: str, db):
        self.db = db
        if os.path.exists(keyfile):
            with open(keyfile, "rb") as f:
                self.master_key = f.read()
        else:
            self.master_key = os.urandom(32)
            with open(keyfile, "wb") as f:
                f.write(self.master_key)
            os.chmod(keyfile, 0o600)

    async def wrap_and_store_dek(self, tenant_id: str, dek: bytes, *, mode: str = "platform") -> None:
        nonce = os.urandom(12)
        wrapped = nonce + AESGCM(self.master_key).encrypt(nonce, dek, tenant_id.encode())
        await self.db.execute(
            """INSERT INTO tenant_keys (tenant_id, wrapped_dek, kms_key_id, algorithm, status, mode)
               VALUES ($1, $2, 'local-file', 'AES-256-GCM', 'active', $3)
               ON CONFLICT (tenant_id)
               DO UPDATE SET wrapped_dek = $2, status = 'active', mode = $3,
                             rotated_at = CURRENT_TIMESTAMP""",
            tenant_id,
            wrapped,
            mode,
        )

    async def unwrap_dek(self, tenant_id: str) -> bytes | None:
        row = await self.db.fetchrow(
            "SELECT wrapped_dek FROM tenant_keys WHERE tenant_id = $1 AND status = 'active'",
            tenant_id,
        )
        if not row:
            return None
        raw = bytes(row["wrapped_dek"])
        nonce, ciphertext = raw[:12], raw[12:]
        return AESGCM(self.master_key).decrypt(nonce, ciphertext, tenant_id.encode())

    async def is_disabled(self, tenant_id: str) -> bool:
        row = await self.db.fetchrow(
            "SELECT status FROM tenant_keys WHERE tenant_id = $1",
            tenant_id,
        )
        return row is not None and row["status"] == "disabled"

    async def set_disabled(self, tenant_id: str) -> None:
        await self.db.execute(
            """INSERT INTO tenant_keys (tenant_id, wrapped_dek, kms_key_id, algorithm, status)
               VALUES ($1, '', 'none', 'none', 'disabled')
               ON CONFLICT (tenant_id)
               DO UPDATE SET status = 'disabled', rotated_at = CURRENT_TIMESTAMP""",
            tenant_id,
        )

    async def remove_key(self, tenant_id: str) -> None:
        await self.db.execute(
            "DELETE FROM tenant_keys WHERE tenant_id = $1", tenant_id
        )

    async def get_mode(self, tenant_id: str) -> str | None:
        row = await self.db.fetchrow(
            "SELECT mode FROM tenant_keys WHERE tenant_id = $1 AND status = 'active'",
            tenant_id,
        )
        return row["mode"] if row else None

    async def set_mode(self, tenant_id: str, mode: str) -> None:
        await self.db.execute(
            "UPDATE tenant_keys SET mode = $1 WHERE tenant_id = $2",
            mode, tenant_id,
        )

    async def store_sealed_key(self, tenant_id: str, public_key_pem: bytes, *, origin: str = "server") -> None:
        await self.db.execute(_SEALED_UPSERT, tenant_id, public_key_pem, f"sealed-{origin}")

    async def get_sealed_public_key(self, tenant_id: str) -> bytes | None:
        row = await self.db.fetchrow(_SEALED_SELECT, tenant_id)
        return bytes(row["wrapped_dek"]) if row else None


class VaultTransitKMS(KMSProvider):
    """HashiCorp Vault Transit secrets engine.

    In this mode, Vault holds the encryption keys. The DEK is generated
    by Vault transit and stored wrapped in tenant_keys. Vault never exposes
    the raw key material — encrypt/decrypt happen via Vault API.
    """

    def __init__(self, url: str, token: str, key_name: str, db):
        self.db = db
        self.url = url.rstrip("/")
        self.token = token
        self.key_name = key_name

    async def _ensure_transit_key(self, name: str) -> None:
        """Create a transit key if it doesn't exist."""
        import httpx

        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.url}/v1/transit/keys/{name}",
                headers={"X-Vault-Token": self.token},
                json={"type": "aes256-gcm96"},
            )

    async def wrap_and_store_dek(self, tenant_id: str, dek: bytes, *, mode: str = "platform") -> None:
        import httpx

        key_name = f"{self.key_name}-{tenant_id}"
        await self._ensure_transit_key(key_name)

        plaintext_b64 = base64.b64encode(dek).decode()
        ctx = base64.b64encode(tenant_id.encode()).decode()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.url}/v1/transit/encrypt/{key_name}",
                headers={"X-Vault-Token": self.token},
                json={"plaintext": plaintext_b64, "context": ctx},
            )
            resp.raise_for_status()
            ciphertext = resp.json()["data"]["ciphertext"]
        await self.db.execute(
            """INSERT INTO tenant_keys (tenant_id, wrapped_dek, kms_key_id, algorithm, status, mode)
               VALUES ($1, $2, $3, 'vault-transit', 'active', $4)
               ON CONFLICT (tenant_id)
               DO UPDATE SET wrapped_dek = $2, status = 'active', mode = $4,
                             rotated_at = CURRENT_TIMESTAMP""",
            tenant_id,
            ciphertext.encode(),
            key_name,
            mode,
        )

    async def unwrap_dek(self, tenant_id: str) -> bytes | None:
        row = await self.db.fetchrow(
            "SELECT wrapped_dek, kms_key_id FROM tenant_keys WHERE tenant_id = $1 AND status = 'active'",
            tenant_id,
        )
        if not row:
            return None
        import httpx

        ciphertext = bytes(row["wrapped_dek"]).decode()
        key_name = row["kms_key_id"]
        ctx = base64.b64encode(tenant_id.encode()).decode()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.url}/v1/transit/decrypt/{key_name}",
                headers={"X-Vault-Token": self.token},
                json={"ciphertext": ciphertext, "context": ctx},
            )
            resp.raise_for_status()
            plaintext_b64 = resp.json()["data"]["plaintext"]
        return base64.b64decode(plaintext_b64)

    async def is_disabled(self, tenant_id: str) -> bool:
        row = await self.db.fetchrow(
            "SELECT status FROM tenant_keys WHERE tenant_id = $1",
            tenant_id,
        )
        return row is not None and row["status"] == "disabled"

    async def set_disabled(self, tenant_id: str) -> None:
        await self.db.execute(
            """INSERT INTO tenant_keys (tenant_id, wrapped_dek, kms_key_id, algorithm, status)
               VALUES ($1, '', 'none', 'none', 'disabled')
               ON CONFLICT (tenant_id)
               DO UPDATE SET status = 'disabled', rotated_at = CURRENT_TIMESTAMP""",
            tenant_id,
        )

    async def remove_key(self, tenant_id: str) -> None:
        await self.db.execute(
            "DELETE FROM tenant_keys WHERE tenant_id = $1", tenant_id
        )

    async def get_mode(self, tenant_id: str) -> str | None:
        row = await self.db.fetchrow(
            "SELECT mode FROM tenant_keys WHERE tenant_id = $1 AND status = 'active'",
            tenant_id,
        )
        return row["mode"] if row else None

    async def set_mode(self, tenant_id: str, mode: str) -> None:
        await self.db.execute(
            "UPDATE tenant_keys SET mode = $1 WHERE tenant_id = $2",
            mode, tenant_id,
        )

    async def store_sealed_key(self, tenant_id: str, public_key_pem: bytes, *, origin: str = "server") -> None:
        await self.db.execute(_SEALED_UPSERT, tenant_id, public_key_pem, f"sealed-{origin}")

    async def get_sealed_public_key(self, tenant_id: str) -> bytes | None:
        row = await self.db.fetchrow(_SEALED_SELECT, tenant_id)
        return bytes(row["wrapped_dek"]) if row else None
