"""Chat controller — shared logic for API and CLI chat.

Both the FastAPI router and the CLI REPL delegate to this controller
for agent resolution, session management, history loading, and persistence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

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
    tenant_id: str | None = None


class ChatController:
    """Shared chat logic used by the API router and the CLI."""

    def __init__(self, db: Database, encryption: EncryptionService):
        self.db = db
        self.encryption = encryption

    async def resolve_agent(self, agent_name: str, *, user_id: UUID | None = None) -> AgentAdapter:
        """Load an agent by schema name. Raises ValueError if not found."""
        return await AgentAdapter.from_schema_name(agent_name, self.db, self.encryption, user_id=user_id)

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
        """Return (session_id, session). Creates if needed; upserts name/type if provided."""
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
        tenant_id: str | None = None,
        name_prefix: str = "chat",
        session_name: str | None = None,
        session_type: str | None = None,
        added_instruction: str | None = None,
    ) -> ChatContext:
        """Resolve agent, session, history, and build the agent — everything before the run."""
        adapter = await self.resolve_agent(agent_name, user_id=user_id)

        sid, session = await self.get_or_create_session(
            session_id,
            agent_name=agent_name, user_id=user_id,
            name_prefix=name_prefix, session_name=session_name, session_type=session_type,
        )

        extra_sections = [added_instruction] if added_instruction else None
        injector = adapter.build_injector(
            user_id=user_id, user_email=user_email, user_name=user_name,
            session_id=str(sid),
            session_name=session.name,
            session_metadata=session.metadata,
            extra_sections=extra_sections,
        )
        message_history = await adapter.load_history(
            sid, user_id=user_id, tenant_id=tenant_id,
        )
        agent = adapter.build_agent()

        return ChatContext(
            adapter=adapter, session_id=sid, agent=agent,
            injector=injector, message_history=message_history,
            tenant_id=tenant_id,
        )

    async def run_turn(
        self,
        ctx: ChatContext,
        user_prompt: str,
        *,
        user_id: UUID | None = None,
        background_compaction: bool = True,
    ) -> ChatTurn:
        """Run a single agent turn and persist the result.

        Raises on agent errors — callers (CLI, API) handle as appropriate.
        """
        result = await ctx.agent.run(
            user_prompt,
            message_history=ctx.message_history or None,
            instructions=ctx.injector.instructions,
        )
        assistant_text = str(result.output) if hasattr(result, "output") else str(result.data)  # type: ignore[attr-defined]
        all_messages = (
            result.all_messages() if hasattr(result, "all_messages")
            else getattr(result, "_all_messages", None)
        )

        await ctx.adapter.persist_turn(
            ctx.session_id, user_prompt, assistant_text,
            user_id=user_id, tenant_id=ctx.tenant_id,
            all_messages=all_messages,
            background_compaction=background_compaction,
        )
        return ChatTurn(assistant_text=assistant_text, all_messages=all_messages)

    async def run_turn_stream(
        self,
        ctx: ChatContext,
        user_prompt: str,
        *,
        user_id: UUID | None = None,
        background_compaction: bool = True,
    ) -> AsyncIterator[str]:
        """Run an agent turn with streaming, yielding text deltas as they arrive.

        After all deltas are yielded, persists the full turn (user + assistant).
        Tool calls are handled internally by pydantic-ai between streamed text chunks.
        """
        accumulated: list[str] = []
        all_messages: list[ModelMessage] | None = None

        async with ctx.agent.iter(
            user_prompt,
            message_history=ctx.message_history or None,
            instructions=ctx.injector.instructions,
        ) as agent_run:
            async for node in agent_run:
                if Agent.is_model_request_node(node):
                    async with node.stream(agent_run.ctx) as request_stream:
                        async for event in request_stream:
                            if isinstance(event, PartStartEvent):
                                if isinstance(event.part, TextPart) and event.part.content:
                                    accumulated.append(event.part.content)
                                    yield event.part.content
                            elif isinstance(event, PartDeltaEvent):
                                if isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
                                    accumulated.append(event.delta.content_delta)
                                    yield event.delta.content_delta
                elif Agent.is_call_tools_node(node):
                    # Execute tools silently (consume the stream to drive execution)
                    async with node.stream(agent_run.ctx) as tools_stream:
                        async for _ in tools_stream:
                            pass

            all_messages = agent_run.result.all_messages() if agent_run.result else None

        assistant_text = "".join(accumulated)
        await ctx.adapter.persist_turn(
            ctx.session_id, user_prompt, assistant_text,
            user_id=user_id, tenant_id=ctx.tenant_id,
            all_messages=all_messages,
            background_compaction=background_compaction,
        )

    async def persist_turn(
        self,
        ctx: ChatContext,
        user_prompt: str,
        assistant_text: str,
        *,
        user_id: UUID | None = None,
        all_messages: list[ModelMessage] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int | None = None,
        model: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Persist a turn without running the agent (used by API streaming)."""
        await ctx.adapter.persist_turn(
            ctx.session_id, user_prompt, assistant_text,
            user_id=user_id, tenant_id=ctx.tenant_id,
            all_messages=all_messages,
            input_tokens=input_tokens, output_tokens=output_tokens,
            latency_ms=latency_ms, model=model, agent_name=agent_name,
        )
