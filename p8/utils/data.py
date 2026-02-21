"""Data generation and seeding utilities.

Reusable helpers for creating sessions, persisting messages, and generating
synthetic conversation data. Used by tests, simulations, demos, and seed
scripts to avoid duplicating boilerplate across the codebase.

Examples::

    from p8.utils.data import create_session, seed_messages

    # Create a session and seed it with 10 alternating messages
    session = await create_session(db, encryption, name="demo-session")
    messages = await seed_messages(memory, session.id, count=10, token_count=50)

    # Seed from a list of message dicts (e.g. loaded from JSON fixtures)
    raw = [
        {"role": "user",      "content": "Hello",      "tokens": 5},
        {"role": "assistant", "content": "Hi there!",  "tokens": 8},
    ]
    messages = await seed_messages_from_dicts(memory, session.id, raw)
"""

from __future__ import annotations

from uuid import UUID, uuid4

from p8.ontology.types import Message, Session
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.tokens import estimate_tokens


async def create_session(
    db: Database,
    encryption: EncryptionService,
    *,
    name: str | None = None,
    mode: str = "chat",
    total_tokens: int = 0,
    user_id: UUID | None = None,
    agent_name: str | None = None,
) -> Session:
    """Create and persist a session, returning the stored row.

    Generates a unique name if none is provided. Uses ``Repository.upsert()``
    so all triggers (KV sync, etc.) fire normally.

    Args:
        db: Database connection.
        encryption: Encryption service for the repository.
        name: Session name. Defaults to ``test-session-<uuid4>``.
        mode: Session mode — ``chat``, ``workflow``, or ``eval``.
        total_tokens: Initial token count (usually 0).
        user_id: Optional user scope.
        agent_name: Optional agent name to stamp on the session.

    Returns:
        The persisted ``Session`` instance with its generated ``id``.

    Examples::

        session = await create_session(db, encryption)
        session = await create_session(db, encryption, name="my-demo", mode="eval")
        session = await create_session(db, encryption, agent_name="general")
    """
    repo = Repository(Session, db, encryption)
    session = Session(
        name=name or f"test-session-{uuid4()}",
        mode=mode,
        total_tokens=total_tokens,
        user_id=user_id,
        agent_name=agent_name,
    )
    [result] = await repo.upsert(session)
    return result


async def seed_messages(
    memory: MemoryService,
    session_id: UUID,
    count: int,
    *,
    token_count: int = 50,
    prefix: str = "msg",
    user_id: UUID | None = None,
    tenant_id: str | None = None,
) -> list[Message]:
    """Persist ``count`` alternating user/assistant messages.

    Generates synthetic content with identifiable prefixes so test assertions
    can locate specific messages. Each message gets exactly ``token_count``
    tokens (passed explicitly to ``persist_message``, not estimated).

    Args:
        memory: MemoryService instance.
        session_id: Target session.
        count: Number of messages to create.
        token_count: Tokens per message (default 50).
        prefix: Content prefix for identification (e.g. ``"batch0"``).
        user_id: Optional user scope.
        tenant_id: Optional tenant scope for encryption.

    Returns:
        List of persisted ``Message`` instances in creation order.

    Examples::

        # 10 messages, 50 tokens each → 500 total session tokens
        msgs = await seed_messages(memory, session.id, 10)

        # 4 messages, 30 tokens each, labeled "batch2"
        msgs = await seed_messages(memory, session.id, 4, token_count=30, prefix="batch2")
    """
    results = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"{prefix}-{i}: " + ("x" * (token_count * 4))
        msg = await memory.persist_message(
            session_id,
            role,
            content,
            token_count=token_count,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        results.append(msg)
    return results


async def seed_messages_from_dicts(
    memory: MemoryService,
    session_id: UUID,
    messages: list[dict],
    *,
    user_id: UUID | None = None,
    tenant_id: str | None = None,
) -> list[Message]:
    """Persist messages from a list of ``{role, content, tokens}`` dicts.

    Useful for replaying conversations from JSON fixtures or seed files.
    Token count defaults to ``estimate_tokens(content)`` if not provided.

    Args:
        memory: MemoryService instance.
        session_id: Target session.
        messages: List of dicts with ``role``, ``content``, and optional ``tokens``.
        user_id: Optional user scope.
        tenant_id: Optional tenant scope for encryption.

    Returns:
        List of persisted ``Message`` instances in creation order.

    Examples::

        raw = json.load(open("tests/data/conversations.json"))
        convo = raw["conversations"][0]
        msgs = await seed_messages_from_dicts(memory, session.id, convo["messages"])
    """
    results = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        tokens = msg.get("tokens", estimate_tokens(content))
        result = await memory.persist_message(
            session_id,
            role,
            content,
            token_count=tokens,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        results.append(result)
    return results
