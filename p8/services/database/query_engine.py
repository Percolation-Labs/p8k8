"""REM dialect parser + query engine.

Parses text-based REM queries (e.g. ``LOOKUP "sarah-chen"``) into structured
``RemQuery`` objects and dispatches them to the appropriate Database.rem_*() method.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from uuid import UUID

log = logging.getLogger(__name__)

# SQL keywords that are never allowed in raw SQL mode
_SQL_BLOCKLIST = re.compile(
    r"\b(DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
# DELETE without WHERE is blocked
_BARE_DELETE = re.compile(
    r"\bDELETE\s+FROM\s+\w+\s*(?:;|\s*$)",
    re.IGNORECASE,
)


@dataclass
class RemQuery:
    """Parsed REM dialect query."""

    mode: str  # LOOKUP | SEARCH | FUZZY | TRAVERSE | SQL
    params: dict = field(default_factory=dict)


class RemQueryParser:
    """Parse a REM dialect string into a ``RemQuery``.

    Syntax (first token selects mode)::

        LOOKUP <key>[, <key2>, ...]
        FUZZY  <query_text> [THRESHOLD <f>] [LIMIT <n>]
        SEARCH <query_text> [FROM <table>] [FIELD <name>] [LIMIT <n>] [MIN_SIMILARITY <f>]
        TRAVERSE <start_key> [DEPTH <n>] [TYPE <rel>]
        SQL <raw_sql>

    Quoted strings are handled via ``shlex.split``.
    ``=``-style kwargs are supported: ``SEARCH "topic" table=resources limit=5``
    Anything that doesn't start with a known keyword is treated as raw SQL.
    """

    _MODES = {"LOOKUP", "SEARCH", "FUZZY", "TRAVERSE", "SQL"}

    # Clause keywords per mode → param name
    _CLAUSES: dict[str, dict[str, str]] = {
        "FUZZY": {"THRESHOLD": "threshold", "LIMIT": "limit"},
        "SEARCH": {
            "FROM": "table",
            "FIELD": "field",
            "LIMIT": "limit",
            "MIN_SIMILARITY": "min_similarity",
        },
        "TRAVERSE": {"DEPTH": "max_depth", "TYPE": "rel_type"},
    }

    # Which params are numeric (float or int)
    _FLOAT_PARAMS = {"threshold", "min_similarity"}
    _INT_PARAMS = {"limit", "max_depth"}

    # Alias map for =style kwargs → canonical param name
    _KWARG_ALIASES: dict[str, str] = {
        "table": "table",
        "field": "field",
        "limit": "limit",
        "threshold": "threshold",
        "min_similarity": "min_similarity",
        "depth": "max_depth",
        "max_depth": "max_depth",
        "type": "rel_type",
        "rel_type": "rel_type",
    }

    def parse(self, query_string: str) -> RemQuery:
        """Parse *query_string* into a :class:`RemQuery`."""
        query_string = query_string.strip()
        if not query_string:
            raise ValueError("Empty query string")

        try:
            tokens = shlex.split(query_string)
        except ValueError:
            # Unmatched quotes — treat as raw SQL
            return RemQuery(mode="SQL", params={"sql": query_string})

        first = tokens[0].upper()

        if first not in self._MODES:
            # Entire string is raw SQL
            return RemQuery(mode="SQL", params={"sql": query_string})

        if first == "SQL":
            # Everything after SQL keyword is the raw query
            raw = query_string[len(tokens[0]):].strip()
            return RemQuery(mode="SQL", params={"sql": raw})

        if first == "LOOKUP":
            return self._parse_lookup(tokens[1:])

        # SEARCH, FUZZY, TRAVERSE — positional arg + optional clauses
        return self._parse_claused(first, tokens[1:])

    # ------------------------------------------------------------------

    def _parse_lookup(self, tokens: list[str]) -> RemQuery:
        if not tokens:
            raise ValueError("LOOKUP requires at least one key")
        # Join remaining non-clause tokens as a single string, then split on commas
        raw = " ".join(tokens)
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if len(keys) == 1:
            return RemQuery(mode="LOOKUP", params={"key": keys[0]})
        return RemQuery(mode="LOOKUP", params={"keys": keys})

    def _parse_claused(self, mode: str, tokens: list[str]) -> RemQuery:
        clauses = self._CLAUSES.get(mode, {})
        params: dict = {}
        positional_parts: list[str] = []
        i = 0

        while i < len(tokens):
            tok = tokens[i]

            # Check =style kwargs (e.g. table=resources)
            if "=" in tok and not tok.startswith("="):
                key, _, val = tok.partition("=")
                param_name = self._KWARG_ALIASES.get(key.lower())
                if param_name:
                    params[param_name] = self._coerce(param_name, val)
                    i += 1
                    continue

            upper = tok.upper()
            if upper in clauses:
                param_name = clauses[upper]
                if i + 1 < len(tokens):
                    params[param_name] = self._coerce(param_name, tokens[i + 1])
                    i += 2
                    continue
                else:
                    raise ValueError(f"{upper} clause requires a value")
            else:
                positional_parts.append(tok)
            i += 1

        # Set the positional argument
        positional = " ".join(positional_parts)
        if not positional:
            raise ValueError(f"{mode} requires a positional argument")

        if mode == "SEARCH":
            params["query_text"] = positional
        elif mode == "FUZZY":
            params["query_text"] = positional
        elif mode == "TRAVERSE":
            params["start_key"] = positional

        return RemQuery(mode=mode, params=params)

    def _coerce(self, param_name: str, value: str):
        if param_name in self._FLOAT_PARAMS:
            return float(value)
        if param_name in self._INT_PARAMS:
            return int(value)
        return value


class RemQueryEngine:
    """Execute parsed REM queries against a Database instance.

    Composes a parser + dispatcher. For SEARCH queries, auto-embeds the query
    text using a provider constructed from settings.

    Usage::

        engine = RemQueryEngine(db, settings)
        results = await engine.execute("SEARCH 'database migration' FROM resources LIMIT 10")
    """

    def __init__(self, db, settings, *, _embedding_provider=None):
        self.db = db
        self.settings = settings
        self._embedding_provider = _embedding_provider
        self.parser = RemQueryParser()

    async def execute(
        self,
        query_string: str,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
    ) -> list[dict]:
        """Parse and execute a REM dialect query string."""
        parsed = self.parser.parse(query_string)
        return await self._dispatch(parsed, tenant_id=tenant_id, user_id=user_id)

    async def _dispatch(
        self,
        query: RemQuery,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
    ) -> list[dict]:
        mode = query.mode
        p = query.params

        if mode == "LOOKUP":
            keys = p.get("keys") or [p["key"]]
            results = []
            for key in keys:
                results.extend(
                    await self.db.rem_lookup(key, tenant_id=tenant_id, user_id=user_id)
                )
            return results

        if mode == "FUZZY":
            return await self.db.rem_fuzzy(  # type: ignore[no-any-return]
                p["query_text"],
                tenant_id=tenant_id,
                user_id=user_id,
                threshold=p.get("threshold", 0.3),
                limit=p.get("limit", 10),
            )

        if mode == "SEARCH":
            table = p.get("table", "schemas")
            # Resolve default field from model's __embedding_field__ if not explicitly provided
            search_field = p.get("field")
            if not search_field:
                from p8.ontology.types import TABLE_MAP
                model = TABLE_MAP.get(table)
                search_field = getattr(model, "__embedding_field__", "content") if model else "content"
            embedding = await self._get_embedding(p["query_text"])
            return await self.db.rem_search(  # type: ignore[no-any-return]
                embedding,
                table,
                field=search_field,
                tenant_id=tenant_id,
                user_id=user_id,
                provider=self._get_provider().provider_name,
                min_similarity=p.get("min_similarity", 0.7),
                limit=p.get("limit", 10),
            )

        if mode == "TRAVERSE":
            return await self.db.rem_traverse(  # type: ignore[no-any-return]
                p["start_key"],
                tenant_id=tenant_id,
                user_id=user_id,
                max_depth=p.get("max_depth", 1),
                rel_type=p.get("rel_type"),
            )

        if mode == "SQL":
            sql = p["sql"]
            self._validate_sql(sql)
            rows = await self.db.fetch(sql)
            return [dict(r) for r in rows]

        raise ValueError(f"Unknown query mode: {mode}")

    async def _get_embedding(self, text: str) -> list[float]:
        provider = self._get_provider()
        vectors = await provider.embed([text])
        return vectors[0]  # type: ignore[no-any-return]

    def _get_provider(self):
        if self._embedding_provider is None:
            from p8.services.embeddings import create_provider

            self._embedding_provider = create_provider(self.settings)
        return self._embedding_provider

    @staticmethod
    def _validate_sql(sql: str) -> None:
        """Reject dangerous SQL statements."""
        if _SQL_BLOCKLIST.search(sql):
            raise ValueError(
                f"Blocked SQL keyword detected. Only SELECT/INSERT/UPDATE queries are allowed."
            )
        if _BARE_DELETE.search(sql):
            raise ValueError("DELETE without WHERE clause is not allowed.")
