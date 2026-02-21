"""Seed script for Jamie Rivera test profile.

Creates a complete 7-day test dataset with 14 sessions, 80+ messages,
22 moments (all 8 types), 6 files, and 2 reminders.

Two modes:
  - DB mode (default): Direct Repository upserts with backdated timestamps.
  - API mode: HTTP calls for end-to-end validation.

Idempotent via deterministic IDs — re-running is safe.

Usage:
    cd p8k8
    uv run python tests/data/fixtures/jamie_rivera/seed.py --mode db
    uv run python tests/data/fixtures/jamie_rivera/seed.py --mode api --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

# ── Constants ────────────────────────────────────────────────────
FIXTURES_DIR = Path(__file__).parent
P8_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "p8.dev")


def det_id(table: str, key: str, user_id: UUID | None = None) -> UUID:
    """Deterministic UUID5 matching p8.ontology.base.deterministic_id."""
    composite = f"{table}:{key}:{str(user_id) if user_id else ''}"
    return uuid.uuid5(P8_NAMESPACE, composite)


def session_id(name: str) -> UUID:
    """Deterministic session UUID from name (sessions have no __id_fields__)."""
    return uuid.uuid5(P8_NAMESPACE, f"seed-session:{name}")


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _days_ago(n: int, time_str: str = "0000") -> datetime:
    """Build a datetime N days ago at HH:MM."""
    now = datetime.now()
    base = datetime(now.year, now.month, now.day) - timedelta(days=n)
    h, m = int(time_str[:2]), int(time_str[2:])
    return base.replace(hour=h, minute=m)


# ── DB Mode ──────────────────────────────────────────────────────


async def seed_db():
    """Seed via direct Repository upserts."""
    import os
    os.environ.setdefault("P8_EMBEDDING_MODEL", "local")
    os.environ.setdefault("P8_EMBEDDING_WORKER_ENABLED", "false")
    os.environ.setdefault("P8_OTEL_ENABLED", "false")

    from p8.ontology.types import File, Message, Moment, Session, Tenant, User
    from p8.services.bootstrap import bootstrap_services
    from p8.services.memory import MemoryService
    from p8.services.repository import Repository

    async with bootstrap_services() as (db, encryption, settings, *_rest):
        memory = MemoryService(db, encryption)

        # ── 1. Tenant + User ────────────────────────────────────
        profile = _load("profile.json")
        tenant_data = profile["tenant"]
        tenant = Tenant(name=tenant_data["name"], encryption_mode=tenant_data["encryption_mode"])
        tenant_repo = Repository(Tenant, db, encryption)
        [tenant] = await tenant_repo.upsert(tenant)
        tenant_id = str(tenant.id)
        print(f"  Tenant: {tenant.name} ({tenant.id})")

        # Configure tenant encryption
        await encryption.configure_tenant(tenant_id, enabled=True, mode="platform")

        user_data = profile["user"]
        user = User(
            name=user_data["name"],
            email=user_data["email"],
            interests=user_data["interests"],
            activity_level=user_data["activity_level"],
            content=user_data["content"],
            tenant_id=tenant_id,
        )
        user_repo = Repository(User, db, encryption)
        [user] = await user_repo.upsert(user)
        user_id = user.id
        print(f"  User: {user.name} ({user.id})")

        # ── 2. Sessions ─────────────────────────────────────────
        sessions_data = _load("sessions.json")["sessions"]
        session_repo = Repository(Session, db, encryption)
        session_map: dict[str, UUID] = {}

        for s in sessions_data:
            sid = session_id(s["name"])
            sess = Session(
                id=sid,
                name=s["name"],
                description=s["description"],
                agent_name=s["agent_name"],
                mode=s["mode"],
                user_id=user_id,
                tenant_id=tenant_id,
            )
            [result] = await session_repo.upsert(sess)
            session_map[s["name"]] = result.id

            # Backdate created_at
            ts = _days_ago(s["days_ago"], "0800")
            await db.execute(
                "UPDATE sessions SET created_at = $1, updated_at = $1 WHERE id = $2",
                ts, result.id,
            )

        print(f"  Sessions: {len(session_map)} created")

        # ── 3. Messages ──────────────────────────────────────────
        conversations = _load("conversations.json")["conversations"]
        total_messages = 0

        for sess_name, msgs in conversations.items():
            sid = session_map[sess_name]
            # Find days_ago for this session
            sess_info = next(s for s in sessions_data if s["name"] == sess_name)
            base_ts = _days_ago(sess_info["days_ago"], "0900")

            for i, msg in enumerate(msgs):
                msg_ts = base_ts + timedelta(minutes=i * 3)
                m = Message(
                    session_id=sid,
                    message_type=msg["message_type"],
                    content=msg["content"],
                    tool_calls=msg.get("tool_calls"),
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
                msg_repo = Repository(Message, db, encryption)
                [result] = await msg_repo.upsert(m)

                # Backdate
                await db.execute(
                    "UPDATE messages SET created_at = $1, updated_at = $1 WHERE id = $2",
                    msg_ts, result.id,
                )
                total_messages += 1

        print(f"  Messages: {total_messages} created")

        # ── 4. Files ─────────────────────────────────────────────
        files_data = _load("files.json")["files"]
        file_repo = Repository(File, db, encryption)

        for f in files_data:
            file_entity = File(
                name=f["name"],
                uri=f["uri"],
                mime_type=f["mime_type"],
                size_bytes=f["size_bytes"],
                parsed_content=f["parsed_content"],
                processing_status=f["processing_status"],
                user_id=user_id,
                tenant_id=tenant_id,
            )
            [result] = await file_repo.upsert(file_entity)

            # Backdate to session time
            sess_info = next(s for s in sessions_data if s["name"] == f["source_session"])
            ts = _days_ago(sess_info["days_ago"], "0830")
            await db.execute(
                "UPDATE files SET created_at = $1, updated_at = $1 WHERE id = $2",
                ts, result.id,
            )

        print(f"  Files: {len(files_data)} created")

        # ── 5. Moments ───────────────────────────────────────────
        moments_data = _load("moments.json")["moments"]
        moment_repo = Repository(Moment, db, encryption)

        for m in moments_data:
            source_sid = session_map[m["source_session"]]
            ts = _days_ago(m["days_ago"], m.get("time", "1200"))

            moment = Moment(
                name=m["name"],
                moment_type=m["moment_type"],
                summary=m["summary"],
                source_session_id=source_sid,
                starts_timestamp=ts,
                ends_timestamp=ts + timedelta(minutes=30),
                image_uri=m.get("image_uri"),
                present_persons=m.get("present_persons", []),
                topic_tags=m.get("topic_tags", []),
                metadata=m.get("metadata", {}),
                user_id=user_id,
                tenant_id=tenant_id,
            )
            [result] = await moment_repo.upsert(moment)

            # Backdate
            await db.execute(
                "UPDATE moments SET created_at = $1, updated_at = $1 WHERE id = $2",
                ts, result.id,
            )

        print(f"  Moments: {len(moments_data)} created")

        # ── 6. Verify feed ───────────────────────────────────────
        feed = await db.rem_moments_feed(user_id=user_id, limit=50)
        day_groups = set()
        for item in feed:
            ed = item.get("event_date")
            if ed:
                day_groups.add(str(ed))

        print(f"\n  Feed verification:")
        print(f"    Total items: {len(feed)}")
        print(f"    Day groups: {len(day_groups)} — {sorted(day_groups, reverse=True)}")
        print(f"    Moment types: {set(item.get('moment_type') for item in feed)}")


# ── API Mode ─────────────────────────────────────────────────────


async def seed_api(base_url: str):
    """Seed via HTTP API calls."""
    import httpx

    profile = _load("profile.json")
    user_data = profile["user"]

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        # 1. Signup / create user
        resp = await client.post("/auth/signup", json={
            "email": user_data["email"],
            "name": user_data["name"],
        })
        if resp.status_code in (200, 201, 409):
            print(f"  User: {user_data['name']} (signup: {resp.status_code})")
        else:
            print(f"  User signup failed: {resp.status_code} {resp.text}")
            return

        # Resolve user ID from email
        uid = str(det_id("users", user_data["email"]))
        headers = {"x-user-id": uid}

        # 2. Sessions + messages via chat
        conversations = _load("conversations.json")["conversations"]
        sessions_data = _load("sessions.json")["sessions"]

        for sess_name, msgs in conversations.items():
            sess_info = next(s for s in sessions_data if s["name"] == sess_name)
            sid = str(session_id(sess_name))

            for msg in msgs:
                if msg["message_type"] == "user":
                    await client.post(
                        f"/chat/{sid}",
                        json={"content": msg["content"]},
                        headers=headers,
                    )
            print(f"    Session {sess_name}: {len(msgs)} messages")

        # 3. File uploads
        files_data = _load("files.json")["files"]
        uploads_dir = FIXTURES_DIR.parent.parent / "uploads"

        for f in files_data:
            sample_file = uploads_dir / Path(f["uri"]).name
            if sample_file.exists():
                resp = await client.post(
                    "/content/",
                    files={"file": (f["name"], sample_file.read_bytes(), f["mime_type"])},
                    headers=headers,
                )
                print(f"    File {f['name']}: {resp.status_code}")

        print(f"\n  API seeding complete. Verify with:")
        print(f"    curl {base_url}/moments/feed?limit=50 -H 'x-user-id: {uid}'")


# ── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Seed Jamie Rivera test profile")
    parser.add_argument("--mode", choices=["db", "api"], default="db",
                        help="Seeding mode (default: db)")
    parser.add_argument("--base-url", default="http://localhost:8000",
                        help="API base URL for api mode")
    args = parser.parse_args()

    print(f"Seeding Jamie Rivera profile ({args.mode} mode)...\n")

    if args.mode == "db":
        asyncio.run(seed_db())
    else:
        asyncio.run(seed_api(args.base_url))

    print("\nDone!")


if __name__ == "__main__":
    main()
