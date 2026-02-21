"""Envelope encryption with pluggable KMS backend.

Encryption modes per tenant:
  platform — we encrypt at rest, decrypt on API read (transparent to client)
  client   — we encrypt at rest with tenant key, API returns ciphertext,
             client decrypts locally (they have access to the key via KMS)
  sealed   — we encrypt at rest using tenant's PUBLIC key only (RSA-OAEP
             hybrid). We can never decrypt. Only the tenant's private key can.

Fallback chain:
  1. Tenant has own key (status='active') → use tenant DEK
  2. Tenant has mode='sealed'            → use public key (hybrid encryption)
  3. Tenant has no key row               → fall back to system DEK (platform mode)
  4. Tenant has key with status='disabled' → no encryption
  5. No tenant_id at all                 → no encryption
"""

from __future__ import annotations

import base64
import hashlib
import os
import time

from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes, serialization

from p8.ontology.base import CoreModel
from p8.services.kms import KMSProvider

# Sentinels for DEK cache
_DISABLED = b"__disabled__"
_SEALED = b"__sealed__"


class EncryptionService:
    def __init__(self, kms: KMSProvider, *, system_tenant_id: str = "__system__", cache_ttl: int = 300):
        self.kms = kms
        self.system_tenant_id = system_tenant_id
        self._dek_cache: dict[str, tuple[bytes | None, float]] = {}
        self._mode_cache: dict[str, tuple[str, float]] = {}
        self._sealed_cache: dict[str, tuple] = {}  # tenant → (public_key_obj, expiry)
        self.cache_ttl = cache_ttl

    async def ensure_system_key(self) -> None:
        """Create system DEK if it doesn't exist. Call once at startup."""
        await self.get_dek(self.system_tenant_id)

    async def get_dek(self, tenant_id: str) -> bytes | None:
        """Resolve DEK with fallback: tenant key → system key → None (disabled/sealed)."""
        cached = self._dek_cache.get(tenant_id)
        if cached and cached[1] > time.time():
            return None if cached[0] is _DISABLED or cached[0] is _SEALED else cached[0]

        # Check if tenant explicitly disabled encryption
        disabled = await self.kms.is_disabled(tenant_id)
        if disabled:
            self._dek_cache[tenant_id] = (_DISABLED, time.time() + self.cache_ttl)
            return None

        # Check sealed mode (asymmetric — public key only, no symmetric DEK)
        mode = await self.kms.get_mode(tenant_id)
        if mode == "sealed":
            await self._get_sealed_pubkey(tenant_id)
            self._dek_cache[tenant_id] = (_SEALED, time.time() + self.cache_ttl)
            return None

        # Try tenant's own key
        dek = await self.kms.unwrap_dek(tenant_id)
        if dek is not None:
            self._dek_cache[tenant_id] = (dek, time.time() + self.cache_ttl)
            return dek

        # System tenant always generates its own key (no further fallback)
        if tenant_id == self.system_tenant_id:
            dek = AESGCM.generate_key(bit_length=256)
            await self.kms.wrap_and_store_dek(tenant_id, dek, mode="platform")
            self._dek_cache[tenant_id] = (dek, time.time() + self.cache_ttl)
            return dek

        # Fall back to system DEK — cache under this tenant too
        dek = await self.get_dek(self.system_tenant_id)
        if dek is not None:
            self._dek_cache[tenant_id] = (dek, time.time() + self.cache_ttl)
        return dek

    async def get_tenant_mode(self, tenant_id: str | None) -> str:
        """Return 'platform', 'client', 'sealed', or 'none' for this tenant."""
        if not tenant_id:
            return "none"

        cached = self._mode_cache.get(tenant_id)
        if cached and cached[1] > time.time():
            return cached[0]

        mode = await self.kms.get_mode(tenant_id)
        if mode is None:
            # No own key — fallback to system = platform mode
            mode = "platform"
        self._mode_cache[tenant_id] = (mode, time.time() + self.cache_ttl)
        return mode

    async def should_decrypt_on_read(self, tenant_id: str | None) -> bool:
        """Platform mode: we decrypt. Client/sealed mode: return ciphertext."""
        mode = await self.get_tenant_mode(tenant_id)
        return mode not in ("client", "sealed")

    async def configure_tenant(
        self, tenant_id: str, *, enabled: bool = True, own_key: bool = False, mode: str = "platform"
    ) -> None:
        """Configure tenant encryption.

        enabled=True, own_key=True  → generate and store a new DEK for this tenant
        enabled=True, own_key=False → use system DEK (no row in tenant_keys)
        enabled=False               → disable encryption, store disabled marker
        mode='platform'             → we decrypt on API read (transparent)
        mode='client'               → API returns ciphertext, client decrypts
        """
        if not enabled:
            await self.kms.set_disabled(tenant_id)
            self._dek_cache.pop(tenant_id, None)
            self._mode_cache.pop(tenant_id, None)
            return

        if own_key:
            dek = AESGCM.generate_key(bit_length=256)
            await self.kms.wrap_and_store_dek(tenant_id, dek, mode=mode)
            self._dek_cache[tenant_id] = (dek, time.time() + self.cache_ttl)
            self._mode_cache[tenant_id] = (mode, time.time() + self.cache_ttl)
        else:
            # Remove any existing key row so fallback to system kicks in
            await self.kms.remove_key(tenant_id)
            self._dek_cache.pop(tenant_id, None)
            self._mode_cache.pop(tenant_id, None)

    # --- Sealed mode (asymmetric hybrid) ---

    async def configure_tenant_sealed(
        self, tenant_id: str, *, public_key_pem: bytes | None = None
    ) -> bytes | None:
        """Configure sealed mode (asymmetric hybrid encryption).

        public_key_pem=None  → server generates RSA-4096 key pair, returns private key PEM once
        public_key_pem=bytes → tenant provides their public key, nothing returned
        """
        if public_key_pem is not None:
            # Tenant-provided public key
            pub_key = serialization.load_pem_public_key(public_key_pem)
            await self.kms.store_sealed_key(tenant_id, public_key_pem, origin="tenant")
            self._sealed_cache[tenant_id] = (pub_key, time.time() + self.cache_ttl)
            self._dek_cache[tenant_id] = (_SEALED, time.time() + self.cache_ttl)
            self._mode_cache[tenant_id] = ("sealed", time.time() + self.cache_ttl)
            return None

        # Server-generated key pair
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        pub_key = private_key.public_key()
        pub_pem = pub_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        priv_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        await self.kms.store_sealed_key(tenant_id, pub_pem, origin="server")
        self._sealed_cache[tenant_id] = (pub_key, time.time() + self.cache_ttl)
        self._dek_cache[tenant_id] = (_SEALED, time.time() + self.cache_ttl)
        self._mode_cache[tenant_id] = ("sealed", time.time() + self.cache_ttl)
        return priv_pem  # return to tenant ONCE — never stored by server

    async def _get_sealed_pubkey(self, tenant_id: str):
        """Load and cache the tenant's public key for sealed mode."""
        cached = self._sealed_cache.get(tenant_id)
        if cached and cached[1] > time.time():
            return cached[0]
        pub_pem = await self.kms.get_sealed_public_key(tenant_id)
        if pub_pem is None:
            return None
        pub_key = serialization.load_pem_public_key(pub_pem)
        self._sealed_cache[tenant_id] = (pub_key, time.time() + self.cache_ttl)
        return pub_key

    # --- Field encryption / decryption ---

    def encrypt_fields(
        self, model_class: type[CoreModel], data: dict, tenant_id: str | None
    ) -> dict:
        encrypted_fields = getattr(model_class, "__encrypted_fields__", {})
        if not encrypted_fields or not tenant_id:
            return data

        cached = self._dek_cache.get(tenant_id)
        if not cached or cached[0] is _DISABLED:
            return data

        # Sealed mode: hybrid encryption with RSA public key
        if cached[0] is _SEALED:
            return self._encrypt_fields_sealed(model_class, data, tenant_id)

        dek = cached[0]
        assert isinstance(dek, bytes)
        entity_id = str(data.get("id", ""))
        aad = f"{tenant_id}:{entity_id}".encode()

        for field, mode in encrypted_fields.items():
            if field not in data or data[field] is None:
                continue
            plaintext = str(data[field]).encode("utf-8")
            if mode == "deterministic":
                nonce = hashlib.sha256(dek + plaintext + aad).digest()[:12]
            else:
                nonce = os.urandom(12)
            ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
            data[field] = base64.b64encode(nonce + ciphertext).decode("ascii")

        return data

    def _encrypt_fields_sealed(
        self, model_class: type[CoreModel], data: dict, tenant_id: str
    ) -> dict:
        """Hybrid encryption: ephemeral AES DEK per field, wrapped by RSA public key."""
        encrypted_fields = getattr(model_class, "__encrypted_fields__", {})
        cached = self._sealed_cache.get(tenant_id)
        if not cached or cached[1] < time.time():
            return data
        pub_key = cached[0]

        entity_id = str(data.get("id", ""))
        aad = f"{tenant_id}:{entity_id}".encode()
        oaep = asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        )

        for field in encrypted_fields:
            if field not in data or data[field] is None:
                continue
            plaintext = str(data[field]).encode("utf-8")

            # Ephemeral symmetric DEK — used once, then discarded
            ephemeral_dek = AESGCM.generate_key(bit_length=256)
            nonce = os.urandom(12)
            ciphertext = AESGCM(ephemeral_dek).encrypt(nonce, plaintext, aad)

            # Wrap ephemeral DEK with tenant's RSA public key
            wrapped_dek = pub_key.encrypt(ephemeral_dek, oaep)

            # Pack: [2-byte wrapped_dek_len][wrapped_dek][12-byte nonce][ciphertext]
            packed = len(wrapped_dek).to_bytes(2, "big") + wrapped_dek + nonce + ciphertext
            data[field] = base64.b64encode(packed).decode("ascii")

        return data

    def decrypt_fields(
        self, model_class: type[CoreModel], data: dict, tenant_id: str | None
    ) -> dict:
        encrypted_fields = getattr(model_class, "__encrypted_fields__", {})
        if not encrypted_fields or not tenant_id:
            return data

        cached = self._dek_cache.get(tenant_id)
        if not cached or cached[0] is _DISABLED or cached[0] is _SEALED:
            return data  # sealed: can't decrypt without private key
        dek = cached[0]
        assert isinstance(dek, bytes)

        entity_id = str(data.get("id", ""))
        aad = f"{tenant_id}:{entity_id}".encode()

        for field in encrypted_fields:
            if field not in data or data[field] is None:
                continue
            try:
                raw = base64.b64decode(data[field])
                nonce, ciphertext = raw[:12], raw[12:]
                plaintext = AESGCM(dek).decrypt(nonce, ciphertext, aad)
                data[field] = plaintext.decode("utf-8")
            except Exception:
                pass  # not encrypted or corrupted — return as-is

        return data

    @staticmethod
    def decrypt_sealed(
        model_class: type[CoreModel], data: dict, tenant_id: str, private_key_pem: bytes
    ) -> dict:
        """Client-side decryption for sealed mode. Requires the tenant's private key.

        This is a convenience for demos and testing. In production, the client
        implements this in their own stack — the server never holds the private key.
        """
        encrypted_fields = getattr(model_class, "__encrypted_fields__", {})
        if not encrypted_fields:
            return data

        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
        assert isinstance(private_key, RSAPrivateKey)
        entity_id = str(data.get("id", ""))
        aad = f"{tenant_id}:{entity_id}".encode()
        oaep = asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        )

        for field in encrypted_fields:
            if field not in data or data[field] is None:
                continue
            try:
                raw = base64.b64decode(data[field])
                dek_len = int.from_bytes(raw[:2], "big")
                wrapped_dek = raw[2:2 + dek_len]
                nonce = raw[2 + dek_len:2 + dek_len + 12]
                ciphertext = raw[2 + dek_len + 12:]

                ephemeral_dek = private_key.decrypt(wrapped_dek, oaep)
                plaintext = AESGCM(ephemeral_dek).decrypt(nonce, ciphertext, aad)
                data[field] = plaintext.decode("utf-8")
            except Exception:
                pass  # wrong key or corrupted — return as-is

        return data

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()
