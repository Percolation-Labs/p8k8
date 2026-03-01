"""Slack router — Events API webhook, slash commands, interactions.

Endpoints are NOT behind api_key_dep; Slack request signing is verified instead.
All heavy work runs in BackgroundTasks so Slack gets a 200 within 3 seconds.
"""

from __future__ import annotations

import json
import logging
import re
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Form, Request, Response

from p8.services.slack import SlackMessage, SlackService

logger = logging.getLogger(__name__)

router = APIRouter()

# Thread-to-agent mapping: "channel:thread_ts" -> agent_name
_thread_agents: dict[str, str] = {}


def _get_slack(request: Request) -> SlackService | None:
    return getattr(request.app.state, "slack_service", None)


# ---------------------------------------------------------------------------
# Signature verification helper
# ---------------------------------------------------------------------------

async def _verify(request: Request, slack: SlackService) -> bytes | None:
    """Read body and verify Slack signature. Returns body bytes or None."""
    body = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not slack.verify_request(body, ts, sig):
        logger.warning("Slack signature verification failed")
        return None
    return body


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@router.post("/events", include_in_schema=False)
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    slack = _get_slack(request)
    if not slack:
        return Response(content="Slack not configured", status_code=503)

    body = await _verify(request, slack)
    if body is None:
        return Response(content="Invalid signature", status_code=401)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return Response(content="Invalid JSON", status_code=400)

    # URL verification challenge
    if data.get("type") == "url_verification":
        return Response(content=data.get("challenge", ""), media_type="text/plain")

    # Event callback
    if data.get("type") == "event_callback":
        event = data.get("event", {})
        if _should_process(event, data, slack):
            background_tasks.add_task(_handle_event, event, slack)

    return Response(content="OK")


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@router.post("/commands", include_in_schema=False)
async def slack_commands(
    request: Request,
    background_tasks: BackgroundTasks,
    command: str = Form(...),
    text: str = Form(""),
    user_id: str = Form(...),
    channel_id: str = Form(...),
    response_url: str = Form(""),
):
    slack = _get_slack(request)
    if not slack:
        return Response(content="Slack not configured", status_code=503)

    body = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not slack.verify_request(body, ts, sig):
        return Response(content="Invalid signature", status_code=401)

    cmd = command.lstrip("/")
    settings = slack._settings

    if cmd == "chat-with" and text.strip():
        agent_name = text.strip()
    else:
        agent_name = settings.slack_default_agent

    # Post intro and process in background
    background_tasks.add_task(
        _handle_command, slack, agent_name, text, user_id, channel_id,
    )

    return Response(
        content=json.dumps({
            "response_type": "ephemeral",
            "text": f"Starting conversation with *{agent_name}*...",
        }),
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------

@router.post("/interactions", include_in_schema=False)
async def slack_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: str = Form(...),
):
    slack = _get_slack(request)
    if not slack:
        return Response(content="Slack not configured", status_code=503)

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return Response(content="Invalid payload", status_code=400)

    payload_type = data.get("type")
    logger.debug("Slack interaction: %s", payload_type)

    if payload_type == "block_actions":
        background_tasks.add_task(_handle_block_action, slack, data)

    return Response(content="OK")


# ---------------------------------------------------------------------------
# Event processing (runs in background)
# ---------------------------------------------------------------------------

def _should_process(event: dict, data: dict, slack: SlackService) -> bool:
    if event.get("bot_id"):
        return False
    if not event.get("text"):
        return False
    app_id = data.get("api_app_id")
    expected = slack._settings.slack_app_id
    if expected and app_id and app_id != expected:
        return False
    return True


def _thread_key(channel: str, thread_ts: str | None) -> str:
    return f"{channel}:{thread_ts or 'main'}"


async def _handle_event(event: dict, slack: SlackService) -> None:
    try:
        message = SlackMessage(**event)
        logger.info("Slack message from %s in %s", message.user, message.channel)

        settings = slack._settings
        channel = message.channel or ""
        channel_id = message.channel_id or channel
        thread_ts = message.thread_ts or message.ts
        key = _thread_key(channel, thread_ts)
        agent_name = _thread_agents.get(key, settings.slack_default_agent)

        # Post "Thinking..." acknowledgment
        resp = slack.post_message(
            f"_Thinking ({agent_name})..._",
            channel=channel,
            thread_ts=message.ts,
            use_markdown=True,
        )
        response_ts = resp["ts"]

        # Get thread context
        thread_messages = []
        if message.thread_ts:
            thread_messages = slack.get_thread(channel_id, message.thread_ts)

        # Strip bot mentions
        clean_query = re.sub(r"<@[A-Z0-9]+>\s*", "", message.text).strip()

        # Call agent
        agent_response = await _call_agent(
            slack, clean_query, thread_messages, channel, message.user or "", agent_name,
        )

        # Update thinking message with response
        slack.update_message(
            agent_response,
            channel=channel,
            ts=response_ts,
            use_markdown=True,
        )

    except Exception as e:
        logger.exception("Error processing Slack event: %s", e)
        try:
            err_channel = event.get("channel", "")
            slack.post_message(
                f"Sorry, there was an error: {str(e)[:200]}",
                channel=err_channel,
                thread_ts=event.get("ts"),
            )
        except Exception:
            pass


async def _call_agent(
    slack: SlackService,
    query: str,
    thread_messages: list[dict],
    channel: str,
    slack_user_id: str,
    agent_name: str,
) -> str:
    """Resolve user, set tool context, and call the p8 agent."""
    from p8.api.tools import set_tool_context
    from p8.api.tools.ask_agent import ask_agent

    try:
        user_id, tenant_id = await slack.resolve_user(slack_user_id)
    except Exception as e:
        logger.warning("User resolution failed for %s: %s", slack_user_id, e)
        user_id = None

    session_id = uuid4()
    set_tool_context(user_id=user_id, session_id=session_id)

    # Build prompt with thread context
    if thread_messages and len(thread_messages) > 1:
        context_lines = []
        for msg in thread_messages[:-1]:
            context_lines.append(f"[{msg['role']}]: {msg['content']}")
        prompt = "Previous conversation:\n" + "\n".join(context_lines) + f"\n\nUser: {query}"
    else:
        prompt = query

    result = await ask_agent(agent_name=agent_name, input_text=prompt)

    if result.get("status") == "error":
        error = result.get("error", "Unknown error")
        logger.error("Agent %s error: %s", agent_name, error)
        return f"Error: {error}"

    return result.get("text_response", "") or str(result.get("output", ""))


async def _handle_command(
    slack: SlackService,
    agent_name: str,
    text: str,
    user_id: str,
    channel_id: str,
) -> None:
    """Handle slash command in background — post intro, assign agent, process."""
    try:
        intro = f"<@{user_id}> started a conversation with *{agent_name}*"
        if text.strip():
            intro = f"<@{user_id}>: {text}"

        resp = slack.post_message(intro, channel=channel_id, use_markdown=True)
        thread_ts = resp["ts"]
        _thread_agents[_thread_key(channel_id, thread_ts)] = agent_name

        if text.strip():
            agent_response = await _call_agent(
                slack, text.strip(), [], channel_id, user_id, agent_name,
            )
            slack.post_message(
                agent_response, channel=channel_id, thread_ts=thread_ts, use_markdown=True,
            )
    except Exception as e:
        logger.exception("Error handling slash command: %s", e)


def _handle_block_action(slack: SlackService, payload: dict) -> None:
    """Handle block action interactions."""
    actions = payload.get("actions", [])
    channel_id: str = payload.get("channel", {}).get("id", "")
    message_ts = payload.get("message", {}).get("ts")

    for action in actions:
        action_id = action.get("action_id", "")
        if action_id == "feedback_up":
            uid = payload.get("user", {}).get("id")
            slack.post_message(
                f"Thanks for the feedback, <@{uid}>!",
                channel=channel_id,
                thread_ts=message_ts,
            )
