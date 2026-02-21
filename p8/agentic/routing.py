"""Agent routing — lazy routing (default) with pluggable classifiers.

Default strategy: active agent persists across turns until it signals
completion or hits max_turns. Classification only happens on session
start, agent completion, or intent shift.

Agents opt in to the default routing table or provide a custom one
via json_schema.routing_model / routing_max_turns.
"""

from __future__ import annotations

from typing import Any, Protocol

from p8.agentic.types import RoutingState
from pydantic import BaseModel

class AbstractRoutingTable(BaseModel):
    """"
    fill in default routing table or load from settings
    Other routers can be assigned to models via config
    This allows this to be merged into the system prompt or conversation attributions (username, date etc)
    With this escalation or routing logic can be shared accross agents instead of creating explciity routing agent\
    Routing should be a function of user profile  and converation history including latest delegation / active agent
    """
    pass
# ---------------------------------------------------------------------------
# Classifier interface
# ---------------------------------------------------------------------------


class RouterClassifier(Protocol):
    """All classifiers implement this interface, making them swappable."""

    async def classify(
        self,
        message: str,
        profile: dict,
        history: list[dict],
        agent_state: dict,
        available_agents: list[dict],
    ) -> str:
        """Return the schema name of the target agent."""
        ...


# ---------------------------------------------------------------------------
# Default classifier (placeholder — routes to fallback)
# ---------------------------------------------------------------------------


class DefaultClassifier:
    """Placeholder classifier that always returns the fallback agent.

    Override with a real classifier (rule-based, trained model, or LLM)
    by setting ``Router.classifier``.
    """

    async def classify(
        self,
        message: str,
        profile: dict,
        history: list[dict],
        agent_state: dict,
        available_agents: list[dict],
    ) -> str:
        fallback: str = agent_state.get("fallback", "general")
        return fallback


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Lazy router with pluggable classifier.

    Usage::

        router = Router()
        agent_name = await router.route(session_metadata, user_message)
    """

    def __init__(self, classifier: RouterClassifier | None = None):
        self.classifier = classifier or DefaultClassifier()

    async def route(
        self,
        session_metadata: dict,
        message: str,
        *,
        profile: dict | None = None,
        history: list[dict] | None = None,
        available_agents: list[dict] | None = None,
    ) -> str:
        """Determine which agent should handle the next message.

        Reads the routing state from session_metadata["routing"].
        If the active agent is still executing and under max_turns,
        returns it directly (no classification). Otherwise, classifies.
        """
        routing_data = session_metadata.get("routing", {})
        routing = RoutingState.model_validate(routing_data)

        # Lazy routing: keep current agent if executing and under limit
        if not routing.should_reclassify() and routing.active_agent:
            routing.increment_turn()
            session_metadata["routing"] = routing.model_dump()
            return routing.active_agent

        # Classification needed
        agent_name = await self.classifier.classify(
            message,
            profile or {},
            history or [],
            routing.model_dump(),
            available_agents or [],
        )

        routing.activate(agent_name)
        session_metadata["routing"] = routing.model_dump()
        return agent_name


# Default router instance — can be replaced with a custom one
default_router = Router()
