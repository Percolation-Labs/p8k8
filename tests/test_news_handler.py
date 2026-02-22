"""End-to-end test: update Jamie's metadata, run news handler, verify resources.

Usage:
    cd p8k8
    uv run python tests/test_news_handler.py
"""

from __future__ import annotations

import asyncio
import json
import os

# Test-mode env vars (same as seed.py)
os.environ.setdefault("P8_EMBEDDING_MODEL", "local")
os.environ.setdefault("P8_EMBEDDING_WORKER_ENABLED", "false")
os.environ.setdefault("P8_OTEL_ENABLED", "false")


JAMIE_METADATA = {
    "interests": [
        "AI machine learning breakthroughs",
        "product management startup strategy",
        "physics space quantum discoveries",
        "engineering architecture distributed systems",
    ],
    "categories": {
        "AI": {
            "keywords": [
                "AI", "LLM", "machine learning", "neural", "ChatGPT",
                "agent", "transformer", "deep learning", "GPT", "Claude",
            ],
            "weight": 1.5,
            "color": "#3b82f6",
        },
        "Product": {
            "keywords": [
                "product", "roadmap", "startup", "launch", "MVP",
                "user research", "metrics", "OKR", "feature", "growth",
            ],
            "weight": 1.3,
            "color": "#22c55e",
        },
        "Physics": {
            "keywords": [
                "physics", "quantum", "particle", "space", "NASA",
                "telescope", "cosmology", "dark matter", "fusion",
            ],
            "weight": 1.2,
            "color": "#8b5cf6",
        },
        "Engineering": {
            "keywords": [
                "engineering", "architecture", "distributed", "database",
                "kubernetes", "infrastructure", "DevOps", "CI/CD", "API",
            ],
            "weight": 1.1,
            "color": "#f97316",
        },
    },
}


async def main():
    from p8.services.bootstrap import bootstrap_services
    from p8.services.repository import Repository
    from p8.ontology.types import User, Resource
    from p8.workers.handlers.news import NewsHandler
    from p8.workers.processor import WorkerContext
    from p8.services.queue import QueueService

    async with bootstrap_services() as (db, encryption, settings, *_rest):
        queue = QueueService(db)
        ctx = WorkerContext(
            db=db, encryption=encryption, queue=queue,
            worker_id="test-news", tier="small",
        )
        ctx.settings = settings  # type: ignore[attr-defined]

        # ── 1. Find Jamie (by deterministic UUID from seed) ─
        from uuid import UUID
        jamie_id = UUID("66fd910d-beba-56d5-a50a-ffb147ce0569")
        row = await db.fetchrow(
            "SELECT id, name, metadata FROM users WHERE id = $1",
            jamie_id,
        )
        if not row:
            print("ERROR: Jamie not found. Run seed first:")
            print("  uv run python tests/data/fixtures/jamie_rivera/seed.py --mode db")
            return

        user_id = row["id"]
        print(f"User: {row['name']} ({user_id})")
        print(f"Current metadata: {row['metadata']}")

        # ── 2. Update metadata ────────────────────────────
        await db.execute(
            "UPDATE users SET metadata = $1::jsonb WHERE id = $2",
            json.dumps(JAMIE_METADATA),
            user_id,
        )
        print(f"Updated metadata with {len(JAMIE_METADATA['interests'])} interests, "
              f"{len(JAMIE_METADATA['categories'])} categories")

        # ── 3. Run news handler directly ──────────────────
        handler = NewsHandler()
        task = {
            "id": "test-task-001",
            "task_type": "news",
            "user_id": user_id,
            "tenant_id": None,
            "payload": {"trigger": "manual_test"},
        }

        print("\nRunning news handler...")
        result = await handler.handle(task, ctx)
        print(f"\nResult: {result}")

        # ── 4. Verify resources in DB ─────────────────────
        resources = await db.fetch(
            "SELECT id, name, uri, category, tags, metadata "
            "FROM resources WHERE user_id = $1 AND category = 'news' "
            "ORDER BY created_at DESC LIMIT 5",
            user_id,
        )
        print(f"\nResources in DB: {len(resources)} (showing up to 5)")
        for r in resources:
            meta = r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"] or "{}")
            score = meta.get("score", "?")
            feed_cat = meta.get("feed_category", "?")
            print(f"  [{score}] [{feed_cat}] {r['name'][:70]}")
            print(f"    uri={r['uri'][:80] if r['uri'] else 'none'}")

        # ── 5. Verify moment ──────────────────────────────
        moments = await db.fetch(
            "SELECT name, summary, metadata FROM moments "
            "WHERE user_id = $1 AND moment_type = 'digest' "
            "ORDER BY created_at DESC LIMIT 1",
            user_id,
        )
        if moments:
            m = moments[0]
            print(f"\nDigest moment: {m['name']}")
            print(f"  Summary: {m['summary']}")
        else:
            print("\nNo digest moment found")


if __name__ == "__main__":
    asyncio.run(main())
