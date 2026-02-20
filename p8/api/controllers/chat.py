"""Chat controller — shared logic for API and CLI chat.

Both the FastAPI router and the CLI REPL delegate to this controller
for agent resolution, session management, history loading, and persistence.
The transport layer (AG-UI streaming vs terminal I/O) stays in the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from p8.agentic.adapter import AgentAdapter
from p8.agentic.types import ContextInjector
from p8.ontology.types import Session
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository


@dataclass
class ChatTurn:
    """Result of running an agent turn."""

    assistant_text: str
    all_messages: list[ModelMessage] | None = None


@dataclass
class ChatContext:
    """Everything needed to run a chat turn — built by prepare()."""

    adapter: AgentAdapter
    session_id: UUID
    agent: Agent
    injector: ContextInjector
    message_history: list[ModelMessage] = field(default_factory=list)


class ChatController:
    """Shared chat logic used by the API router and the CLI."""

    def __init__(self, db: Database, encryption: EncryptionService):
        self.db = db
        self.encryption = encryption

    async def resolve_agent(
        self,
        agent_name: str,
        *,
        user_id: UUID | None = None,
    ) -> AgentAdapter:
        """Load an agent by schema name. Raises ValueError if not found."""
        return await AgentAdapter.from_schema_name(
            agent_name, self.db, self.encryption, user_id=user_id,
        )

    async def get_or_create_session(
        self,
        session_id: UUID | None,
        *,
        agent_name: str,
        user_id: UUID | None = None,
        name_prefix: str = "chat",
        session_name: str | None = None,
        session_type: str | None = None,
    ) -> tuple[UUID, Session]:
        """Return (session_id, session). Creates if it doesn't exist.

        If *session_name* or *session_type* are provided they are upserted —
        applied on both new and existing sessions.
        """
        repo = Repository(Session, self.db, self.encryption)
        sid = session_id or uuid4()

        existing = await repo.get(sid)
        if existing:
            dirty = False
            if session_name is not None and existing.name != session_name:
                existing.name = session_name
                dirty = True
            if session_type is not None and existing.mode != session_type:
                existing.mode = session_type
                dirty = True
            if dirty:
                await repo.upsert(existing)
            return sid, existing

        session = Session(
            id=sid,
            name=session_name or f"{name_prefix}-{sid}",
            agent_name=agent_name,
            mode=session_type or "chat",
            user_id=user_id,
        )
        await repo.upsert(session)
        return sid, session

    async def prepare(
        self,
        agent_name: str,
        session_id: UUID | None = None,
        *,
        user_id: UUID | None = None,
        user_email: str | None = None,
        user_name: str | None = None,
        name_prefix: str = "chat",
        session_name: str | None = None,
        session_type: str | None = None,
    ) -> ChatContext:
        """Resolve agent, session, context, history, build agent — everything before the run."""
        adapter = await self.resolve_agent(agent_name, user_id=user_id)

        sid, session = await self.get_or_create_session(
            session_id,
            agent_name=agent_name,
            user_id=user_id,
            name_prefix=name_prefix,
            session_name=session_name,
            session_type=session_type,
        )

        # Extract session context for injection
        session_name = session.name if session else None
        session_metadata = session.metadata if session else None

        injector = adapter.build_injector(
            user_id=user_id,
            user_email=user_email,
            user_name=user_name,
            session_id=str(sid),
            session_name=session_name,
            session_metadata=session_metadata,
        )

        message_history = await adapter.load_history(sid, user_id=user_id)

        agent = adapter.build_agent()

        return ChatContext(
            adapter=adapter,
            session_id=sid,
            agent=agent,
            injector=injector,
            message_history=message_history,
        )

    async def run_turn(
        self,
        ctx: ChatContext,
        user_prompt: str,
        *,
        user_id: UUID | None = None,
        background_compaction: bool = True,
    ) -> ChatTurn:
        """Run a single agent turn and persist the result."""
        try:
            result = await ctx.agent.run(
                user_prompt,
                message_history=ctx.message_history or None,
                instructions=ctx.injector.instructions,
            )
            assistant_text = str(result.output) if hasattr(result, "output") else str(result.data)

            all_messages = None
            if hasattr(result, "all_messages"):
                all_messages = result.all_messages()
            elif hasattr(result, "_all_messages"):
                all_messages = result._all_messages

        except Exception as e:
            assistant_text = f"[error] {e}"
            all_messages = None

        await ctx.adapter.persist_turn(
            ctx.session_id,
            user_prompt,
            assistant_text,
            user_id=user_id,
            all_messages=all_messages,
            background_compaction=background_compaction,
        )

        return ChatTurn(assistant_text=assistant_text, all_messages=all_messages)

    async def persist_turn(
        self,
        ctx: ChatContext,
        user_prompt: str,
        assistant_text: str,
        *,
        user_id: UUID | None = None,
        all_messages: list[ModelMessage] | None = None,
    ) -> None:
        """Persist a turn without running the agent (used by API streaming)."""
        await ctx.adapter.persist_turn(
            ctx.session_id,
            user_prompt,
            assistant_text,
            user_id=user_id,
            all_messages=all_messages,
        )
