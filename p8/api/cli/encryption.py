"""p8 encryption — inspect and test tenant encryption."""

from __future__ import annotations

import asyncio
from typing import Optional

import typer

import p8.services.bootstrap as _svc
from p8.ontology.types import Message, User
from p8.services.repository import Repository

encryption_app = typer.Typer(no_args_is_help=True)


async def _status():
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        typer.echo(f"  provider:      {settings.kms_provider}")
        if settings.kms_provider == "vault":
            typer.echo(f"  vault url:     {settings.kms_vault_url}")
            typer.echo(f"  transit key:   {settings.kms_vault_transit_key}")
        else:
            typer.echo(f"  keyfile:       {settings.kms_local_keyfile}")

        rows = await db.fetch(
            "SELECT tenant_id, kms_key_id, algorithm, status, mode FROM tenant_keys ORDER BY tenant_id"
        )
        typer.echo(f"\n  {len(rows)} tenant key(s):\n")
        typer.echo(f"  {'TENANT':<28s} {'MODE':<12s} {'STATUS':<10s} {'ALGORITHM':<16s} {'KMS KEY'}")
        typer.echo(f"  {'-'*28} {'-'*12} {'-'*10} {'-'*16} {'-'*24}")
        for r in rows:
            typer.echo(
                f"  {r['tenant_id']:<28s} {r['mode'] or '-':<12s} "
                f"{r['status']:<10s} {r['algorithm']:<16s} {r['kms_key_id']}"
            )


async def _configure(tenant_id: str, mode: str, own_key: bool):
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        if mode == "disabled":
            await encryption.configure_tenant(tenant_id, enabled=False)
            typer.echo(f"  {tenant_id}: encryption disabled")
        elif mode == "sealed":
            private_pem = await encryption.configure_tenant_sealed(tenant_id)
            typer.echo(f"  {tenant_id}: sealed mode (server-generated key)")
            if private_pem:
                typer.echo(f"  private key ({len(private_pem)} bytes) — save this, shown once:")
                typer.echo(private_pem.decode())
        else:
            await encryption.configure_tenant(tenant_id, enabled=True, own_key=own_key, mode=mode)
            key_type = "own key" if own_key else "system key"
            typer.echo(f"  {tenant_id}: {mode} mode ({key_type})")


async def _test(tenant_id: str, mode: str):
    """Round-trip test: configure tenant, store message, verify DB/API behavior."""
    from uuid import uuid4

    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        # Configure
        if mode == "disabled":
            await encryption.configure_tenant(tenant_id, enabled=False)
        else:
            await encryption.configure_tenant(tenant_id, enabled=True, own_key=True, mode=mode)

        typer.echo(f"  mode:     {mode}")
        typer.echo(f"  tenant:   {tenant_id}")

        # Create session + message
        sid = uuid4()
        await db.execute(
            "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)"
            " ON CONFLICT DO NOTHING",
            sid, f"enc-test-{mode}", tenant_id,
        )
        plaintext = f"Encryption test: mode={mode}, tenant={tenant_id}"
        repo = Repository(Message, db, encryption)
        msg = Message(session_id=sid, message_type="user", content=plaintext, tenant_id=tenant_id)
        [saved] = await repo.upsert(msg)

        # Check DB (raw)
        raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
        db_is_encrypted = raw["content"] != plaintext
        typer.echo(f"  db raw:   {'[ciphertext]' if db_is_encrypted else plaintext}")
        typer.echo(f"  enc lvl:  {saved.encryption_level}")

        # Check API-style read (mode-aware)
        loaded = await repo.get_for_tenant(saved.id, tenant_id=tenant_id)
        assert loaded is not None, "Message not found after upsert"
        api_is_plain = loaded.content == plaintext
        typer.echo(f"  api read: {loaded.content if api_is_plain else '[ciphertext]'}")

        # Verify expectations
        ok = True
        if mode == "disabled":
            ok = not db_is_encrypted and api_is_plain
        elif mode == "platform":
            ok = db_is_encrypted and api_is_plain
        elif mode == "client":
            ok = db_is_encrypted and not api_is_plain

        typer.echo(f"  result:   {'PASS' if ok else 'FAIL'}")

        # Cleanup
        await db.execute("DELETE FROM messages WHERE id = $1", saved.id)
        await db.execute("DELETE FROM sessions WHERE id = $1", sid)
        if not ok:
            raise typer.Exit(1)


async def _test_isolation():
    """Verify cross-tenant decryption fails."""
    from uuid import uuid4

    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        tenant_a, tenant_b = "iso-test-a", "iso-test-b"
        await encryption.configure_tenant(tenant_a, enabled=True, own_key=True)
        await encryption.configure_tenant(tenant_b, enabled=True, own_key=True)

        sid = uuid4()
        await db.execute(
            "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)",
            sid, "iso-test", tenant_a,
        )
        repo = Repository(Message, db, encryption)
        msg = Message(session_id=sid, message_type="user", content="secret-a", tenant_id=tenant_a)
        [saved] = await repo.upsert(msg)

        loaded_a = await repo.get(saved.id, tenant_id=tenant_a)
        loaded_b = await repo.get(saved.id, tenant_id=tenant_b)
        assert loaded_a is not None, "Message not found for tenant A"
        assert loaded_b is not None, "Message not found for tenant B"

        ok = loaded_a.content == "secret-a" and loaded_b.content != "secret-a"
        typer.echo(f"  tenant-a reads: {loaded_a.content}")
        typer.echo(f"  tenant-b reads: {'[garbled]' if loaded_b.content != 'secret-a' else 'secret-a (LEAK!)'}")
        typer.echo(f"  isolation:      {'PASS' if ok else 'FAIL'}")

        # Cleanup
        await db.execute("DELETE FROM messages WHERE id = $1", saved.id)
        await db.execute("DELETE FROM sessions WHERE id = $1", sid)
        await encryption.configure_tenant(tenant_a, enabled=True, own_key=False)
        await encryption.configure_tenant(tenant_b, enabled=True, own_key=False)
        if not ok:
            raise typer.Exit(1)


@encryption_app.command("status")
def status_command():
    """Show KMS provider and tenant keys."""
    asyncio.run(_status())


@encryption_app.command("configure")
def configure_command(
    tenant_id: str = typer.Argument(help="Tenant ID"),
    mode: str = typer.Option("platform", "--mode", "-m", help="platform | client | sealed | disabled"),
    own_key: bool = typer.Option(True, "--own-key/--system-key", help="Dedicated DEK vs system fallback"),
):
    """Configure encryption for a tenant."""
    asyncio.run(_configure(tenant_id, mode, own_key))


@encryption_app.command("test")
def test_command(
    tenant_id: str = typer.Option("enc-test-tenant", "--tenant", "-t", help="Tenant ID for test"),
    mode: str = typer.Option("platform", "--mode", "-m", help="platform | client | disabled"),
):
    """Round-trip encryption test: store, check DB, check API read."""
    asyncio.run(_test(tenant_id, mode))


@encryption_app.command("test-isolation")
def test_isolation_command():
    """Verify cross-tenant decryption fails."""
    asyncio.run(_test_isolation())
