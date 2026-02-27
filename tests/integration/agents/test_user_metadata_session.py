"""Multi-turn agent session test for user metadata accumulation.

Proves that the general agent can call update_user_metadata across
consecutive turns, with partial updates accumulating without overwriting.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from p8.agentic.adapter import AgentAdapter
from p8.api.controllers.chat import ChatController
from p8.api.mcp_server import create_mcp_server
from p8.api.tools import init_tools, set_tool_context
from p8.ontology.types import User
from p8.services.repository import Repository


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


def _make_agent_fn():
    """Build a FunctionModel callback that simulates a general agent.

    Recognises user prompts by keyword and returns the corresponding
    update_user_metadata tool call.  After a tool result, returns
    a friendly confirmation.
    """

    def agent_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # After a tool completes, return confirmation text
        for msg in reversed(messages):
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart(content="Got it, noted!")])

        # Find the latest user prompt
        user_text = ""
        for msg in reversed(messages):
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, UserPromptPart):
                        user_text = part.content.lower()
                        break
                if user_text:
                    break

        # Dispatch tool calls based on prompt content
        if "bonnie" in user_text or "cat" in user_text:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="update_user_metadata",
                args={
                    "metadata": {
                        "relations": [{"name": "Bonnie", "role": "pet", "notes": "cat"}],
                    },
                },
                tool_call_id="tc-1",
            )])
        elif "knitting" in user_text:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="update_user_metadata",
                args={
                    "metadata": {
                        "interests": ["knitting"],
                    },
                },
                tool_call_id="tc-2",
            )])
        elif "physics" in user_text or "arxiv" in user_text:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="update_user_metadata",
                args={
                    "metadata": {
                        "feeds": [{
                            "url": "https://arxiv.org/list/physics/new",
                            "name": "arXiv Physics",
                            "type": "website",
                        }],
                    },
                },
                tool_call_id="tc-3",
            )])
        else:
            return ModelResponse(parts=[TextPart(
                content="I remember everything about you!",
            )])

    return agent_fn


async def test_multi_turn_metadata_accumulation_and_recovery(db, encryption):
    """Full lifecycle: save metadata across 3 turns, then recover on a fresh session.

    Session 1 (saving):
      Turn 1: "I have a cat called Bonnie"  → saves relation
      Turn 2: "I'm interested in knitting"  → saves interest
      Turn 3: "Keep an eye on physics papers at arxiv" → saves feed URL

    Session 2 (recovery — fresh, no message history):
      Turn 4: "What's my pet's name?"
        → agent calls user_profile tool → gets metadata → answers "Bonnie"
      Turn 5: "What are my hobbies and what feeds am I following?"
        → agent calls user_profile tool → gets metadata → answers knitting + arXiv

    This proves metadata persists in the DB and is recoverable from a
    completely new session with no conversation history.
    """
    init_tools(db, encryption)
    mcp = create_mcp_server()

    # Create test user
    repo = Repository(User, db, encryption)
    user = User(name="session-test-user", email="session@test.dev", metadata={})
    [user] = await repo.upsert(user)
    set_tool_context(user_id=user.id)

    # --- Phase 1: Save metadata across 3 turns ---

    agent_fn = _make_agent_fn()
    original_build = AgentAdapter.build_agent

    def save_patched_build(self, **kwargs):
        return original_build(self, model_override=FunctionModel(agent_fn), mcp_server=mcp)

    controller = ChatController(db, encryption)

    with patch.object(AgentAdapter, "build_agent", save_patched_build):
        # Turn 1 — cat named Bonnie
        ctx = await controller.prepare("general", user_id=user.id)
        turn1 = await controller.run_turn(
            ctx, "I have a cat called Bonnie",
            user_id=user.id, background_compaction=False,
        )
        assert "noted" in turn1.assistant_text.lower() or "got it" in turn1.assistant_text.lower()

        # Verify after turn 1: relations saved
        u = await repo.get(user.id)
        assert u.metadata.get("relations") is not None
        assert u.metadata["relations"][0]["name"] == "Bonnie"

        # Turn 2 — interested in knitting
        ctx = await controller.prepare("general", user_id=user.id)
        turn2 = await controller.run_turn(
            ctx, "I'm really interested in knitting",
            user_id=user.id, background_compaction=False,
        )
        assert "noted" in turn2.assistant_text.lower() or "got it" in turn2.assistant_text.lower()

        # Verify after turn 2: interests added, relations NOT overwritten
        u = await repo.get(user.id)
        assert u.metadata["relations"][0]["name"] == "Bonnie", "relations clobbered by turn 2"
        assert "knitting" in u.metadata["interests"]

        # Turn 3 — physics papers feed
        ctx = await controller.prepare("general", user_id=user.id)
        turn3 = await controller.run_turn(
            ctx, "I'd like to keep an eye on physics papers, save https://arxiv.org/list/physics/new",
            user_id=user.id, background_compaction=False,
        )
        assert "noted" in turn3.assistant_text.lower() or "got it" in turn3.assistant_text.lower()

    # Confirm all three metadata keys accumulated in DB
    updated_user = await repo.get(user.id)
    meta = updated_user.metadata
    assert meta["relations"][0]["name"] == "Bonnie"
    assert "knitting" in meta["interests"]
    assert meta["feeds"][0]["url"] == "https://arxiv.org/list/physics/new"

    # --- Phase 2: Fresh session — recover metadata via user_profile tool ---

    # Capture the tool results the agent sees so we can verify them
    captured_profile_results: list[dict] = []

    def _make_recovery_fn(user_email: str):
        """FunctionModel that calls user_profile on any question, then answers from the result."""

        def recovery_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            # After user_profile returns, parse the result and answer
            for msg in reversed(messages):
                if isinstance(msg, ModelRequest):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            content = part.content
                            # MCP tools may wrap result as {"result": "<json>"}
                            if isinstance(content, dict):
                                content = content.get("result", json.dumps(content))
                            profile = json.loads(content)
                            captured_profile_results.append(profile)
                            m = profile.get("metadata", {})
                            # Build answer from the metadata the tool returned
                            answer_parts = []
                            for r in m.get("relations", []):
                                answer_parts.append(
                                    f"Your {r.get('notes', 'pet')} is called {r['name']}"
                                )
                            for interest in m.get("interests", []):
                                answer_parts.append(f"You're into {interest}")
                            for feed in m.get("feeds", []):
                                answer_parts.append(
                                    f"You follow {feed['name']} at {feed['url']}"
                                )
                            return ModelResponse(parts=[TextPart(
                                content=". ".join(answer_parts) + ".",
                            )])

            # First call — user_profile is a zero-arg tool (user_id auto-resolved from context)
            return ModelResponse(parts=[ToolCallPart(
                tool_name="get_user_profile",
                args={},
                tool_call_id="tc-profile",
            )])

        return recovery_fn

    recovery_fn = _make_recovery_fn(user.email)

    def recovery_patched_build(self, **kwargs):
        return original_build(self, model_override=FunctionModel(recovery_fn), mcp_server=mcp)

    with patch.object(AgentAdapter, "build_agent", recovery_patched_build):
        # Turn 4 — FRESH session, ask about pet
        fresh_ctx = await controller.prepare(
            "general", user_id=user.id, user_email=user.email,
        )
        # Confirm this is a different session (no history from saving turns)
        assert fresh_ctx.session_id != ctx.session_id
        assert fresh_ctx.message_history == []

        turn4 = await controller.run_turn(
            fresh_ctx, "What's my pet's name?",
            user_id=user.id, background_compaction=False,
        )

        # The tool returned the full profile — verify it has the metadata
        assert len(captured_profile_results) == 1
        recovered_meta = captured_profile_results[0]["metadata"]
        assert recovered_meta["relations"][0]["name"] == "Bonnie"
        assert "knitting" in recovered_meta["interests"]
        assert recovered_meta["feeds"][0]["url"] == "https://arxiv.org/list/physics/new"

        # The agent's answer is built from the tool result
        assert "Bonnie" in turn4.assistant_text

        # Turn 5 — another fresh session, ask about hobbies + feeds
        captured_profile_results.clear()
        fresh_ctx2 = await controller.prepare(
            "general", user_id=user.id, user_email=user.email,
        )
        assert fresh_ctx2.message_history == []

        turn5 = await controller.run_turn(
            fresh_ctx2, "What are my hobbies and what feeds am I following?",
            user_id=user.id, background_compaction=False,
        )

        assert len(captured_profile_results) == 1
        assert "knitting" in turn5.assistant_text
        assert "arXiv" in turn5.assistant_text or "arxiv" in turn5.assistant_text.lower()

    # --- Phase 3: Partial prune — add a second interest, then remove just one ---

    # 3a. Add a second interest (cooking) alongside the existing one (knitting)
    def add_interest_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for msg in reversed(messages):
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart(content="Got it, noted!")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="update_user_metadata",
            args={"metadata": {"interests": ["knitting", "cooking"]}},
            tool_call_id="tc-add-interest",
        )])

    def add_interest_build(self, **kwargs):
        return original_build(self, model_override=FunctionModel(add_interest_fn), mcp_server=mcp)

    with patch.object(AgentAdapter, "build_agent", add_interest_build):
        ctx_add = await controller.prepare("general", user_id=user.id)
        await controller.run_turn(
            ctx_add, "I'm also into cooking now",
            user_id=user.id, background_compaction=False,
        )

    u = await repo.get(user.id)
    assert set(u.metadata["interests"]) == {"knitting", "cooking"}
    assert u.metadata["relations"][0]["name"] == "Bonnie"  # untouched

    # 3b. Partial prune: drop cooking, keep knitting (agent sends the pruned list)
    def partial_prune_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for msg in reversed(messages):
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart(
                            content="Done, I've removed cooking from your interests.",
                        )])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="update_user_metadata",
            args={"metadata": {"interests": ["knitting"]}},
            tool_call_id="tc-partial-prune",
        )])

    def partial_prune_build(self, **kwargs):
        return original_build(self, model_override=FunctionModel(partial_prune_fn), mcp_server=mcp)

    with patch.object(AgentAdapter, "build_agent", partial_prune_build):
        ctx_pp = await controller.prepare("general", user_id=user.id)
        turn_pp = await controller.run_turn(
            ctx_pp, "Actually I'm not into cooking anymore, remove it",
            user_id=user.id, background_compaction=False,
        )
        assert "cooking" in turn_pp.assistant_text.lower()

    # Verify: knitting survived, cooking gone, everything else untouched
    u = await repo.get(user.id)
    assert u.metadata["interests"] == ["knitting"], f"partial prune failed: {u.metadata['interests']}"
    assert u.metadata["relations"][0]["name"] == "Bonnie"
    assert u.metadata["feeds"][0]["url"] == "https://arxiv.org/list/physics/new"

    # --- Phase 4: Full key prune — remove feeds via remove_keys ---

    def prune_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for msg in reversed(messages):
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart(
                            content="Done, I've removed your feeds.",
                        )])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="update_user_metadata",
            args={"metadata": {}, "remove_keys": ["feeds"]},
            tool_call_id="tc-prune",
        )])

    def prune_patched_build(self, **kwargs):
        return original_build(self, model_override=FunctionModel(prune_fn), mcp_server=mcp)

    with patch.object(AgentAdapter, "build_agent", prune_patched_build):
        prune_ctx = await controller.prepare("general", user_id=user.id)
        turn6 = await controller.run_turn(
            prune_ctx, "Actually, remove my feeds — I don't need them anymore",
            user_id=user.id, background_compaction=False,
        )
        assert "removed" in turn6.assistant_text.lower()

    # Verify feeds gone, relations + interests survived
    pruned_user = await repo.get(user.id)
    pruned_meta = pruned_user.metadata
    assert "feeds" not in pruned_meta, f"feeds not pruned: {pruned_meta}"
    assert pruned_meta["relations"][0]["name"] == "Bonnie"
    assert pruned_meta["interests"] == ["knitting"]

    # --- Phase 5: Fresh session confirms all pruning persisted ---

    captured_profile_results.clear()

    with patch.object(AgentAdapter, "build_agent", recovery_patched_build):
        post_prune_ctx = await controller.prepare(
            "general", user_id=user.id, user_email=user.email,
        )
        assert post_prune_ctx.message_history == []

        turn_final = await controller.run_turn(
            post_prune_ctx, "What do you know about me?",
            user_id=user.id, background_compaction=False,
        )

        assert len(captured_profile_results) == 1
        final_meta = captured_profile_results[0]["metadata"]
        # Full key prune: feeds gone
        assert "feeds" not in final_meta, "feeds still present after prune"
        # Partial prune: cooking gone, knitting survived
        assert final_meta["interests"] == ["knitting"], (
            f"partial prune not reflected: {final_meta['interests']}"
        )
        # Untouched: relations intact
        assert final_meta["relations"][0]["name"] == "Bonnie"
        # Answer: Bonnie + knitting present, arXiv + cooking absent
        assert "Bonnie" in turn_final.assistant_text
        assert "knitting" in turn_final.assistant_text
        assert "arxiv" not in turn_final.assistant_text.lower()
        assert "cooking" not in turn_final.assistant_text.lower()


async def test_metadata_survives_subsequent_upsert(db, encryption):
    """After metadata is set via merge_metadata, a plain upsert doesn't wipe it.

    This is the agent-level version of test_upsert_does_not_clobber_merged_metadata
    from test_user_metadata.py — proves the full pipeline is safe.
    """
    init_tools(db, encryption)
    mcp = create_mcp_server()

    repo = Repository(User, db, encryption)
    user = User(name="upsert-safe-user", email="upsert-safe@test.dev", metadata={})
    [user] = await repo.upsert(user)
    set_tool_context(user_id=user.id)

    agent_fn = _make_agent_fn()
    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(self, model_override=FunctionModel(agent_fn), mcp_server=mcp)

    controller = ChatController(db, encryption)

    with patch.object(AgentAdapter, "build_agent", patched_build):
        ctx = await controller.prepare("general", user_id=user.id)
        await controller.run_turn(
            ctx, "I have a cat called Bonnie",
            user_id=user.id, background_compaction=False,
        )

    # Metadata is set
    u = await repo.get(user.id)
    assert u.metadata["relations"][0]["name"] == "Bonnie"

    # Simulate an unrelated upsert (e.g. name change) — metadata must survive
    user_update = User(id=user.id, name="upsert-safe-user-renamed", email="upsert-safe@test.dev")
    [updated] = await repo.upsert(user_update)

    assert updated.metadata.get("relations") is not None, (
        f"upsert wiped metadata: {updated.metadata}"
    )
    assert updated.metadata["relations"][0]["name"] == "Bonnie"


async def test_tool_calls_persisted_in_messages(db, encryption):
    """update_user_metadata tool calls are persisted as message rows for observability."""
    init_tools(db, encryption)
    mcp = create_mcp_server()

    repo = Repository(User, db, encryption)
    user = User(name="persistence-test-user", email="persist@test.dev", metadata={})
    [user] = await repo.upsert(user)
    set_tool_context(user_id=user.id)

    agent_fn = _make_agent_fn()
    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(self, model_override=FunctionModel(agent_fn), mcp_server=mcp)

    controller = ChatController(db, encryption)

    with patch.object(AgentAdapter, "build_agent", patched_build):
        ctx = await controller.prepare("general", user_id=user.id)
        turn = await controller.run_turn(
            ctx, "I'm interested in knitting",
            user_id=user.id, background_compaction=False,
        )

    # Check persisted messages include tool_call rows
    rows = await db.fetch(
        "SELECT message_type, content, tool_calls FROM messages"
        " WHERE session_id = $1 ORDER BY created_at",
        ctx.session_id,
    )
    types = [r["message_type"] for r in rows]
    assert "user" in types
    assert "tool_call" in types
    assert "tool_response" in types
    assert "assistant" in types

    # The tool_call row should reference update_user_metadata
    tc_rows = [r for r in rows if r["message_type"] == "tool_call"]
    assert len(tc_rows) >= 1
    tc_data = tc_rows[0]["tool_calls"]
    if isinstance(tc_data, str):
        tc_data = json.loads(tc_data)
    assert tc_data["name"] == "update_user_metadata"
