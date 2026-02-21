"""Encryption demo — shows all encryption modes in action.

Demonstrates how message content is encrypted at rest, how different tenants
are isolated, and how platform / client / sealed / disabled modes affect
what the API and DB operator see.

Prerequisites:
    - PostgreSQL running with p8 schema (p8 migrate)
    - .env with P8_DATABASE_URL

Usage:
    python tests/.sim/encryption_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from p8.services.bootstrap import create_kms
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository
from p8.ontology.types import Message, User
from p8.settings import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def header(title: str) -> None:
    width = 70
    print(f"\n{CYAN}{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}{RESET}\n")


def label(name: str, value: str, *, color: str = "") -> None:
    print(f"  {BOLD}{name:<24}{RESET}{color}{value}{RESET}")


def raw_preview(raw: str, *, max_len: int = 72) -> str:
    if raw is None:
        return "<NULL>"
    if len(raw) > max_len:
        return raw[:max_len] + "..."
    return raw


async def make_session(db: Database, name: str, tenant_id: str | None) -> "uuid4":
    """Insert a session row (messages FK to sessions)."""
    sid = uuid4()
    if tenant_id:
        await db.execute(
            "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)",
            sid, name, tenant_id,
        )
    else:
        await db.execute(
            "INSERT INTO sessions (id, name) VALUES ($1, $2)", sid, name,
        )
    return sid


# ---------------------------------------------------------------------------
# Demo scenarios
# ---------------------------------------------------------------------------


async def demo_platform_mode(db: Database, enc: EncryptionService) -> None:
    """Platform mode: server encrypts at rest, decrypts on read. Client sees plaintext."""
    header("1. PLATFORM MODE  (default — transparent encryption)")

    tenant = "acme-corp"
    plaintext = "My SSN is 123-45-6789 and my salary is $185,000."

    # System key covers this tenant (no explicit configure needed)
    await enc.get_dek(tenant)

    session_id = await make_session(db, "platform-demo", tenant)
    repo = Repository(Message, db, enc)

    msg = Message(
        session_id=session_id,
        message_type="user",
        content=plaintext,
        tenant_id=tenant,
    )

    print(f"  {DIM}Storing message for tenant '{tenant}'...{RESET}\n")
    [saved] = await repo.upsert(msg)

    # What the database sees
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    label("Plaintext (input):", plaintext)
    label("DB column (raw):", raw_preview(raw["content"]), color=RED)
    print()
    label("Encrypted?", "YES — ciphertext stored in DB", color=GREEN)
    print()

    # What the API returns (platform mode = decrypt on read)
    loaded = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    label("API response:", loaded.content, color=GREEN)
    label("Mode:", "platform — server decrypts, client sees plaintext", color=DIM)


async def demo_client_mode(db: Database, enc: EncryptionService) -> None:
    """Client mode: server encrypts, API returns ciphertext. Client decrypts locally."""
    header("2. CLIENT MODE  (tenant controls decryption)")

    tenant = "secretive-inc"
    plaintext = "Project codename MIDNIGHT — launch date 2026-04-01."

    # Configure tenant with their own key in client mode
    await enc.configure_tenant(tenant, enabled=True, own_key=True, mode="client")

    session_id = await make_session(db, "client-demo", tenant)
    repo = Repository(Message, db, enc)

    msg = Message(
        session_id=session_id,
        message_type="user",
        content=plaintext,
        tenant_id=tenant,
    )

    print(f"  {DIM}Storing message for tenant '{tenant}' (client mode)...{RESET}\n")
    [saved] = await repo.upsert(msg)

    # DB column
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    label("Plaintext (input):", plaintext)
    label("DB column (raw):", raw_preview(raw["content"]), color=RED)
    print()

    # Mode-aware API response: returns ciphertext
    loaded_raw = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    label("API response:", raw_preview(loaded_raw.content), color=YELLOW)
    label("Mode:", "client — API returns ciphertext", color=DIM)
    print()
    label("Matches DB?", str(loaded_raw.content == raw["content"]), color=GREEN)
    print()

    # Client-side decrypt (simulated — they'd use KMS access in production)
    loaded_dec = await repo.get(saved.id, tenant_id=tenant, decrypt=True)
    label("Client decrypts:", loaded_dec.content, color=GREEN)
    label("Note:", "In production, client calls Vault Transit API to decrypt", color=DIM)


async def demo_disabled(db: Database, enc: EncryptionService) -> None:
    """Disabled: tenant opts out of encryption. Content stored as plaintext."""
    header("3. DISABLED MODE  (opt-out, plaintext storage)")

    tenant = "yolo-startup"
    plaintext = "We don't encrypt anything because we like to live dangerously."

    await enc.configure_tenant(tenant, enabled=False)

    session_id = await make_session(db, "disabled-demo", tenant)
    repo = Repository(Message, db, enc)

    msg = Message(
        session_id=session_id,
        message_type="user",
        content=plaintext,
        tenant_id=tenant,
    )

    print(f"  {DIM}Storing message for tenant '{tenant}' (disabled)...{RESET}\n")
    [saved] = await repo.upsert(msg)

    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    label("Plaintext (input):", plaintext)
    label("DB column (raw):", raw_preview(raw["content"]), color=YELLOW)
    print()
    label("Encrypted?", "NO — plaintext in DB (tenant opted out)", color=RED)

    loaded = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    label("API response:", loaded.content, color=GREEN)


async def demo_tenant_isolation(db: Database, enc: EncryptionService) -> None:
    """Two tenants with their own keys. One cannot read the other's data."""
    header("4. TENANT ISOLATION  (cross-tenant decryption fails)")

    tenant_a = "hospital-a"
    tenant_b = "hospital-b"
    plaintext = "Patient diagnosed with condition XYZ."

    await enc.configure_tenant(tenant_a, enabled=True, own_key=True)
    await enc.configure_tenant(tenant_b, enabled=True, own_key=True)

    session_id = await make_session(db, "isolation-demo", tenant_a)
    repo = Repository(Message, db, enc)

    msg = Message(
        session_id=session_id,
        message_type="user",
        content=plaintext,
        tenant_id=tenant_a,
    )

    print(f"  {DIM}Storing message as '{tenant_a}'...{RESET}\n")
    [saved] = await repo.upsert(msg)

    # Correct tenant decrypts
    loaded_a = await repo.get(saved.id, tenant_id=tenant_a)
    label(f"{tenant_a} reads:", loaded_a.content, color=GREEN)

    # Wrong tenant fails to decrypt (returns ciphertext/garbled)
    loaded_b = await repo.get(saved.id, tenant_id=tenant_b)
    label(f"{tenant_b} reads:", raw_preview(loaded_b.content), color=RED)
    print()

    matches = loaded_b.content == plaintext
    label("Cross-tenant leak?", "NO" if not matches else "YES (BUG!)",
          color=GREEN if not matches else RED)
    label("Why:", "AAD includes tenant_id — wrong key + wrong AAD = failure", color=DIM)


async def demo_deterministic_email(db: Database, enc: EncryptionService) -> None:
    """Deterministic encryption: same email + same key is queryable."""
    header("5. DETERMINISTIC ENCRYPTION  (email exact-match)")

    tenant = "lookup-corp"
    await enc.get_dek(tenant)

    repo = Repository(User, db, enc)

    alice = User(name="Alice", email="alice@example.com", content="Engineer at LookupCorp", tenant_id=tenant)
    bob = User(name="Bob", email="bob@example.com", content="Designer at LookupCorp", tenant_id=tenant)

    print(f"  {DIM}Storing two users with encrypted emails...{RESET}\n")
    [saved_alice] = await repo.upsert(alice)
    [saved_bob] = await repo.upsert(bob)

    # Raw DB values
    raw_a = await db.fetchrow("SELECT email, content FROM users WHERE id = $1", saved_alice.id)
    raw_b = await db.fetchrow("SELECT email, content FROM users WHERE id = $1", saved_bob.id)

    label("Alice email (DB):", raw_preview(raw_a["email"]), color=RED)
    label("Alice bio (DB):", raw_preview(raw_a["content"]), color=RED)
    print()
    label("Bob email (DB):", raw_preview(raw_b["email"]), color=RED)
    label("Bob bio (DB):", raw_preview(raw_b["content"]), color=RED)
    print()

    # Decrypt on read
    loaded_a = await repo.get(saved_alice.id, tenant_id=tenant)
    loaded_b = await repo.get(saved_bob.id, tenant_id=tenant)
    label("Alice email (API):", loaded_a.email, color=GREEN)
    label("Bob email (API):", loaded_b.email, color=GREEN)
    print()
    label("email mode:", "deterministic — same input, same key = same ciphertext", color=DIM)
    label("content mode:", "randomized — same input = different ciphertext each time", color=DIM)


async def demo_no_tenant(db: Database, enc: EncryptionService) -> None:
    """No tenant ID = no encryption. Public/anonymous data."""
    header("6. NO TENANT  (no encryption applied)")

    plaintext = "This is a public announcement."

    session_id = await make_session(db, "public-demo", None)
    repo = Repository(Message, db, enc)

    msg = Message(
        session_id=session_id,
        message_type="system",
        content=plaintext,
    )

    print(f"  {DIM}Storing message with no tenant_id...{RESET}\n")
    [saved] = await repo.upsert(msg)

    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    label("DB column (raw):", raw["content"], color=YELLOW)
    label("Encrypted?", "NO — no tenant_id means no key to encrypt with", color=DIM)


# ---------------------------------------------------------------------------
# Sealed mode demos
# ---------------------------------------------------------------------------


async def demo_sealed_server_key(db: Database, enc: EncryptionService) -> None:
    """Sealed mode (server-generated): server creates RSA pair, returns private key once."""
    header("7. SEALED MODE — SERVER KEY  (we generate pair, hand over private key)")

    tenant = "sealed-server-corp"
    plaintext = "Top secret: acquiring CompanyX for $2B on March 15th."

    print(f"  {DIM}Generating RSA-4096 key pair for tenant '{tenant}'...{RESET}\n")
    private_key_pem = await enc.configure_tenant_sealed(tenant)

    label("Private key:", f"RSA-4096 ({len(private_key_pem)} bytes PEM) — returned to tenant ONCE", color=YELLOW)
    label("Server stores:", "Public key only (in tenant_keys table)", color=GREEN)
    print()

    session_id = await make_session(db, "sealed-server-demo", tenant)
    repo = Repository(Message, db, enc)

    msg = Message(
        session_id=session_id,
        message_type="user",
        content=plaintext,
        tenant_id=tenant,
    )

    print(f"  {DIM}Encrypting with ephemeral AES DEK wrapped by RSA public key...{RESET}\n")
    [saved] = await repo.upsert(msg)

    # DB column — hybrid ciphertext (wrapped DEK + AES ciphertext)
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    label("Plaintext (input):", plaintext)
    label("DB column (raw):", raw_preview(raw["content"]), color=RED)
    print()

    # API response — sealed = always ciphertext (server has no private key)
    loaded = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    label("API response:", raw_preview(loaded.content), color=YELLOW)
    label("Server can decrypt?", "NO — only the public key is stored", color=GREEN)
    print()

    # Client decrypts with their private key
    row_data = dict(await db.fetchrow("SELECT * FROM messages WHERE id = $1", saved.id))
    decrypted = EncryptionService.decrypt_sealed(Message, row_data, tenant, private_key_pem)
    label("Client decrypts:", decrypted["content"], color=GREEN)
    label("How:", "RSA-OAEP unwrap ephemeral DEK, then AES-256-GCM decrypt", color=DIM)


async def demo_sealed_tenant_key(db: Database, enc: EncryptionService) -> None:
    """Sealed mode (tenant-provided): tenant generates key pair, gives us only the public key."""
    header("8. SEALED MODE — TENANT KEY  (tenant generates pair, gives us public key only)")

    tenant = "sealed-tenant-corp"
    plaintext = "Patient records: blood type A+, allergy to penicillin."

    print(f"  {DIM}Tenant generates their own RSA-4096 key pair locally...{RESET}\n")

    # Tenant generates their own key pair (this happens on THEIR side)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    public_key_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    label("Tenant generates:", "RSA-4096 key pair (on their machine)", color=DIM)
    label("Gives us:", f"Public key only ({len(public_key_pem)} bytes PEM)", color=GREEN)
    label("Keeps secret:", "Private key — never leaves tenant infrastructure", color=YELLOW)
    print()

    # We receive only the public key
    await enc.configure_tenant_sealed(tenant, public_key_pem=public_key_pem)

    session_id = await make_session(db, "sealed-tenant-demo", tenant)
    repo = Repository(Message, db, enc)

    msg = Message(
        session_id=session_id,
        message_type="user",
        content=plaintext,
        tenant_id=tenant,
    )

    print(f"  {DIM}Encrypting with tenant's public key...{RESET}\n")
    [saved] = await repo.upsert(msg)

    # DB has hybrid ciphertext
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    label("Plaintext (input):", plaintext)
    label("DB column (raw):", raw_preview(raw["content"]), color=RED)
    print()

    # API returns ciphertext
    loaded = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    label("API response:", raw_preview(loaded.content), color=YELLOW)
    label("Server can decrypt?", "NO — we NEVER had the private key", color=GREEN)
    print()

    # Client decrypts with their private key
    row_data = dict(await db.fetchrow("SELECT * FROM messages WHERE id = $1", saved.id))
    decrypted = EncryptionService.decrypt_sealed(Message, row_data, tenant, private_key_pem)
    label("Client decrypts:", decrypted["content"], color=GREEN)
    label("Difference:", "Server never generated or touched the private key", color=DIM)


# ---------------------------------------------------------------------------
# DB operator view
# ---------------------------------------------------------------------------


async def demo_what_db_operator_sees(db: Database) -> None:
    """Summary: what a DB operator with SELECT access sees."""
    header("9. DB OPERATOR VIEW  (what a rogue operator sees)")

    print(f"  {DIM}SELECT tenant_id, message_type, LEFT(content, 60)")
    print(f"  FROM messages ORDER BY created_at;{RESET}\n")

    rows = await db.fetch(
        "SELECT id, tenant_id, message_type, content FROM messages ORDER BY created_at"
    )

    print(f"  {'TENANT':<22} {'TYPE':<12} {'CONTENT (first 60 chars)'}")
    print(f"  {'-' * 22} {'-' * 12} {'-' * 60}")
    for r in rows:
        tid = r["tenant_id"] or "(none)"
        content = r["content"] or ""
        preview = content[:60] + ("..." if len(content) > 60 else "")
        # Detect likely ciphertext (base64 or long non-plaintext)
        is_plain = content.startswith(("We don't", "This is"))
        color = YELLOW if is_plain else RED
        print(f"  {tid:<22} {r['message_type']:<12} {color}{preview}{RESET}")

    print(f"\n  {BOLD}Result:{RESET} Encrypted rows (platform, client, sealed) show ciphertext.")
    print(f"  {BOLD}       {RESET} Disabled / no-tenant rows are plaintext.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    print(f"\n{BOLD}{CYAN}")
    print("  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║          p8 — Encryption Demo                              ║")
    print("  ║                                                            ║")
    print("  ║  AES-256-GCM envelope encryption with per-tenant keys      ║")
    print("  ║  Modes: platform, client, sealed, disabled                 ║")
    print("  ╚══════════════════════════════════════════════════════════════╝")
    print(f"{RESET}")

    settings = Settings()
    db = Database(settings)
    await db.connect()

    kms = create_kms(settings, db)
    print(f"  {GREEN}KMS: {settings.kms_provider} ({settings.kms_vault_url if settings.kms_provider == 'vault' else settings.kms_local_keyfile}){RESET}")
    enc = EncryptionService(kms, system_tenant_id=settings.system_tenant_id, cache_ttl=settings.dek_cache_ttl)

    # Clean slate for demo (delete ALL tenant keys so they're re-created
    # with the current KMS provider — avoids format mismatch when switching
    # between local and vault providers)
    await db.execute(
        "TRUNCATE messages, users, sessions, feedback CASCADE"
    )
    await db.execute("DELETE FROM tenant_keys")
    await enc.ensure_system_key()

    try:
        await demo_platform_mode(db, enc)
        await demo_client_mode(db, enc)
        await demo_disabled(db, enc)
        await demo_tenant_isolation(db, enc)
        await demo_deterministic_email(db, enc)
        await demo_no_tenant(db, enc)
        await demo_sealed_server_key(db, enc)
        await demo_sealed_tenant_key(db, enc)
        await demo_what_db_operator_sees(db)

        header("SUMMARY")
        print(textwrap.dedent(f"""\
          {BOLD}Encryption Model:{RESET}
            Key hierarchy:  KMS master key  -->  per-tenant DEK  -->  AES-256-GCM per field
            AAD binding:    tenant_id:entity_id (prevents cross-tenant and cross-row attacks)
            Storage:        base64(nonce + ciphertext) in TEXT columns

          {BOLD}Four modes per tenant:{RESET}
            platform   Server encrypts + decrypts. Client sees plaintext. DB sees ciphertext.
            client     Server encrypts. API returns ciphertext. Client decrypts via KMS.
            sealed     Server encrypts with public key only. Server CAN'T decrypt. Ever.
            disabled   Plaintext storage. Tenant accepts the risk.

          {BOLD}Sealed mode sub-variants:{RESET}
            server-key   Server generates RSA-4096 pair, returns private key once. Convenient.
            tenant-key   Tenant generates pair, gives us only the public key. Zero trust.

          {BOLD}Field modes:{RESET}
            randomized      Different ciphertext each time. No equality queries. (content, bios)
            deterministic   Same ciphertext for same input+key. Enables WHERE email = enc(x). (emails)

          {BOLD}What never leaks:{RESET}
            DB operator with SELECT sees only ciphertext (encrypted tenants)
            KV store trigger stores name-only summaries for encrypted tables
            Cross-tenant reads fail due to AAD mismatch
            Sealed mode: even a compromised SERVER can't read stored data
        """))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
