"""Unit tests for tiered entity resolution (§16.2) — mocked DB/HTTP, no network.

Each tier is exercised by making the cheaper tiers miss and asserting the
next one is reached, matching the cascading, cost-ordered design.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from p8.services.percolate.entity_resolution import resolve_entity_name


def _mock_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


class _FakeDb:
    def __init__(self, registry_row=None, search_results=None):
        self._registry_row = registry_row
        self._search_results = search_results or []
        self.fetchrow = AsyncMock(return_value=registry_row)
        self.rem_search = AsyncMock(return_value=self._search_results)


async def test_tier1_registry_match_short_circuits():
    db = _FakeDb(registry_row={"name": "OpenAI", "entity_type": "company", "external_ids": {"wikidata_qid": "Q21708200"}})

    with patch("httpx.AsyncClient") as mock_client_cls:
        resolved = await resolve_entity_name(
            "OpenAI", db=db, encryption=None, embedding_service=None, settings=None,
        )
        mock_client_cls.assert_not_called()

    assert resolved.tier == "registry"
    assert resolved.confidence == 1.0
    assert resolved.name == "OpenAI"


async def test_tier2_wikidata_match_when_registry_misses():
    db = _FakeDb(registry_row=None)
    wikidata_payload = {"search": [{"id": "Q95", "label": "Google", "description": "American tech company"}]}

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("p8.services.percolate.entity_resolution.Repository") as mock_repo_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(wikidata_payload))
        mock_repo_cls.return_value.upsert = AsyncMock(return_value=[])

        resolved = await resolve_entity_name(
            "Google", db=db, encryption=None, embedding_service=None, settings=None,
        )

    assert resolved.tier == "wikidata"
    assert resolved.name == "Google"
    assert resolved.external_ids == {"wikidata_qid": "Q95"}


async def test_tier3_embedding_fallback_when_wikidata_misses():
    db = _FakeDb(
        registry_row=None,
        search_results=[{"entity_type": "percolate_entities", "similarity_score": 0.9,
                          "data": {"name": "Acme Corporation", "entity_type": "company", "external_ids": {}}}],
    )
    settings = MagicMock(percolate_embedding_match_threshold=0.82)
    embedding_service = MagicMock()
    embedding_service.provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    embedding_service.provider.provider_name = "openai"

    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response({"search": []}))

        resolved = await resolve_entity_name(
            "Acme Corp", db=db, encryption=None, embedding_service=embedding_service, settings=settings,
        )

    assert resolved.tier == "embedding"
    assert resolved.name == "Acme Corporation"
    assert resolved.confidence == 0.9


async def test_tier4_llm_last_resort_when_all_else_misses():
    db = _FakeDb(registry_row=None, search_results=[])
    settings = MagicMock(percolate_interpret_model="", default_model="openai:gpt-4.1-nano")

    class _FakeResolution:
        canonical_name = "Very Obscure Startup"
        entity_type = "company"

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(output=_FakeResolution()))

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("pydantic_ai.Agent", return_value=fake_agent), \
         patch("p8.services.percolate.entity_resolution.Repository") as mock_repo_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response({"search": []}))
        mock_repo_cls.return_value.upsert = AsyncMock(return_value=[])

        resolved = await resolve_entity_name(
            "very obscure startup inc.", db=db, encryption=None, embedding_service=None, settings=settings,
        )

    assert resolved.tier == "llm"
    assert resolved.name == "Very Obscure Startup"
    assert resolved.confidence == 0.6
