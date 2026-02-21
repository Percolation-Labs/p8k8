"""POST /chat/{chat_id} — AG-UI streaming chat with child-event multiplexing."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic_ai.ui.ag_ui import AGUIAdapter
from starlette.responses import Response

from ag_ui.core.events import CustomEvent

from p8.agentic.adapter import DEFAULT_AGENT_NAME
from p8.agentic.delegate import set_child_event_sink
from p8.api.controllers.chat import ChatController
from p8.api.deps import get_db, get_encryption, get_optional_user
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.usage import check_quota, get_user_plan, increment_usage
from p8.utils.tokens import estimate_tokens

logger = logging.getLogger(__name__)

router = APIRouter()


def _extract_user_prompt(body: dict) -> str:
    """Extract the last user message content from AG-UI request body."""
    for msg in reversed(body.get("messages", [])):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
            return content  # type: ignore[no-any-return]
    return ""


async def _get_child_event_with_timeout(
    queue: asyncio.Queue,
    timeout: float = 0.05,
) -> dict | None:
    """Read from the child event queue with a short timeout.

    Returns None on timeout (no child event available).
    """
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def _child_event_to_agui(event: dict) -> CustomEvent:
    """Convert a child event dict to an AG-UI CustomEvent."""
    return CustomEvent(
        name=event.get("type", "child_content"),
        value=event,
    )


async def _merged_event_stream(
    agui_stream: AsyncIterator,
    child_sink: asyncio.Queue,
) -> AsyncIterator:
    """Multiplex the parent's AG-UI event stream with child events.

    Uses ``asyncio.wait(FIRST_COMPLETED)`` to race the next parent AG-UI
    event against the next child event from the queue. Whichever arrives
    first is yielded immediately, enabling real-time interleaving of
    child agent content tokens during tool execution.

    When the parent stream ends (StopAsyncIteration), drains any remaining
    child events from the queue before returning.
    """
    parent_iter = agui_stream.__aiter__()
    parent_done = False

    # Start initial tasks
    pending_parent = asyncio.ensure_future(_anext_or_sentinel(parent_iter))
    pending_child = asyncio.ensure_future(
        _get_child_event_with_timeout(child_sink, timeout=0.05)
    )

    while not parent_done:
        # Race parent event vs child event
        done, _ = await asyncio.wait(
            {pending_parent, pending_child},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            if task is pending_parent:
                result = task.result()
                if result is _SENTINEL:
                    parent_done = True
                else:
                    yield result
                    # Schedule next parent event
                    pending_parent = asyncio.ensure_future(
                        _anext_or_sentinel(parent_iter)
                    )

            elif task is pending_child:
                child_event = task.result()
                if child_event is not None:
                    yield _child_event_to_agui(child_event)
                # Schedule next child event (always, even on timeout)
                pending_child = asyncio.ensure_future(
                    _get_child_event_with_timeout(child_sink, timeout=0.05)
                )

    # Cancel pending child task
    pending_child.cancel()
    try:
        await pending_child
    except (asyncio.CancelledError, Exception):
        pass

    # Drain remaining child events
    while True:
        try:
            event = child_sink.get_nowait()
            yield _child_event_to_agui(event)
        except asyncio.QueueEmpty:
            break


_SENTINEL = object()


async def _anext_or_sentinel(aiter):
    """Get next item from async iterator, returning _SENTINEL on StopAsyncIteration."""
    try:
        return await aiter.__anext__()
    except StopAsyncIteration:
        return _SENTINEL


@router.post("/{chat_id}")
async def chat(
    chat_id: str,
    request: Request,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
) -> Response:
    """Streaming chat endpoint with AG-UI protocol.

    Uses AGUIAdapter Option 2 (``run_stream`` + ``streaming_response``)
    with a multiplexer for real-time child agent streaming.

    Headers:
        x-agent-schema-name: Agent schema name (required)
        x-user-id: User identity (optional)
        x-user-email: User email (optional)
        x-user-name: User display name (optional)
        x-session-name: Session display name — upserted on create or update (optional)
        x-session-type: Session mode (chat|workflow|eval) — upserted on create or update (optional)

    Body: AG-UI RunAgentInput — all fields optional (defaults filled from chat_id).
        Supported: thread_id, run_id, messages, tools, context, state, forwarded_props.
    """
    agent_name = request.headers.get("x-agent-schema-name") or DEFAULT_AGENT_NAME

    # Resolve user identity — JWT or x-user-id/x-tenant-id headers
    current_user = await get_optional_user(request)
    user_id = current_user.user_id if current_user else None
    user_email = current_user.email if current_user else None
    user_name = request.headers.get("x-user-name")
    tenant_id = (current_user.tenant_id or None) if current_user else None
    session_name = request.headers.get("x-session-name")
    session_type = request.headers.get("x-session-type")

    controller = ChatController(db, encryption)

    # Parse session ID — accept UUID or arbitrary string (auto-generates UUID)
    try:
        session_uuid = UUID(chat_id) if chat_id else None
    except ValueError:
        session_uuid = None

    try:
        ctx = await controller.prepare(
            agent_name,
            session_id=session_uuid,
            user_id=user_id,
            user_email=user_email,
            user_name=user_name,
            tenant_id=tenant_id,
            session_name=session_name,
            session_type=session_type,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")

    # Pre-flight quota check (only when user is identified)
    plan_id: str | None = None
    if user_id:
        plan_id = await get_user_plan(db, user_id)
        status = await check_quota(db, user_id, "chat_tokens", plan_id)
        if status.exceeded:
            raise HTTPException(
                429,
                detail={
                    "error": "chat_token_quota_exceeded",
                    "used": status.used,
                    "limit": status.limit,
                    "message": "Monthly chat token limit reached. Purchase an add-on at /billing/addon or upgrade your plan.",
                },
            )

    # Parse body and fill in AG-UI defaults so callers can omit them
    raw_body = await request.body()
    body = json.loads(raw_body) if raw_body else {}
    body.setdefault("thread_id", chat_id)
    body.setdefault("threadId", chat_id)
    body.setdefault("run_id", str(uuid4()))
    body.setdefault("runId", body["run_id"])
    body.setdefault("messages", [])
    body.setdefault("tools", [])
    body.setdefault("context", [])
    body.setdefault("forwarded_props", {})
    body.setdefault("forwardedProps", {})
    body.setdefault("state", None)
    # Overwrite cached body so AGUIAdapter.from_request sees the defaults
    request._body = json.dumps(body).encode()
    user_prompt = _extract_user_prompt(body)

    # Create child event sink for delegation streaming
    child_sink: asyncio.Queue = asyncio.Queue()
    previous_sink = set_child_event_sink(child_sink)

    stream_start = time.monotonic()

    async def on_complete(result):
        """Persist messages and track token usage after streaming completes."""
        try:
            assistant_text = str(result.output) if hasattr(result, "output") else str(result.data)
            all_messages = None
            if hasattr(result, "all_messages"):
                all_messages = result.all_messages()
            elif hasattr(result, "_all_messages"):
                all_messages = result._all_messages

            # Extract usage from pydantic-ai result
            usage = result.usage() if hasattr(result, "usage") else None
            input_tokens = usage.input_tokens if usage and usage.input_tokens else 0
            output_tokens = usage.output_tokens if usage and usage.output_tokens else 0
            latency_ms = int((time.monotonic() - stream_start) * 1000)
            model_name = ctx.adapter.config.get_options().get("model", "")
            agent_name_val = ctx.adapter.schema.name

            await controller.persist_turn(
                ctx,
                user_prompt,
                assistant_text,
                user_id=user_id,
                all_messages=all_messages,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                model=model_name,
                agent_name=agent_name_val,
            )

            # Post-flight: increment chat token usage
            if user_id and plan_id:
                token_estimate = estimate_tokens(user_prompt) + estimate_tokens(assistant_text)
                await increment_usage(db, user_id, "chat_tokens", max(token_estimate, 1), plan_id)
        except Exception:
            logger.exception("Failed to persist turn or track usage")
        finally:
            # Restore previous event sink
            set_child_event_sink(previous_sink)

    try:
        # Option 2: build adapter, run_stream, wrap with multiplexer
        adapter = await AGUIAdapter.from_request(request, agent=ctx.agent)

        agui_stream = adapter.run_stream(
            message_history=ctx.message_history or None,
            on_complete=on_complete,
            instructions=ctx.injector.instructions,
        )

        # Wrap with multiplexer to interleave child events
        merged_stream = _merged_event_stream(agui_stream, child_sink)

        return adapter.streaming_response(merged_stream)
    except Exception:
        # Clean up event sink on error
        set_child_event_sink(previous_sink)
        raise
