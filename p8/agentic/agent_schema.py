"""
AgentSchema — Flat, Unified Schema for Declarative Agents
==========================================================

Single-level schema combining JSON Schema fields with agent config.
No nested ``json_schema_extra`` wrapper — everything is a top-level field.

STRUCTURE
---------
An agent schema is a flat dict (or YAML doc) with two groups of fields:

**JSON Schema standard:**  type, description, properties, required

**Agent config:**  name, tools, model, temperature, limits, structured_output, ...

Example YAML::

    type: object
    description: |
      You are a helpful assistant...
    properties:
      user_intent:
        type: string
        description: "Classify: question, task, greeting, follow-up"
    name: general
    tools:
      - name: search
        description: Query knowledge base using REM dialect
      - name: action

TOOLS
-----
Tools are ``{name, server, description}`` dicts:

- **name**: Tool function name on the MCP server
- **server**: Server alias; omit or ``null`` for local (defaults to local)
- **description**: Optional suffix appended to the tool's base description,
  giving this agent context-specific guidance

PROPERTIES AS THINKING AIDES
-----------------------------
In conversational mode (``structured_output: false``, the default), fields
are NOT the agent's output — they're **internal scaffolding** that guides
the LLM's reasoning.  Each field description tells the model what to
observe and track while formulating its response.

In structured mode (``structured_output: true``), the model MUST return a
JSON object matching the properties schema.  Use for background processors
like the DreamingAgent where output maps to database entities.

USAGE
-----
    from p8.agentic.agent_schema import AgentSchema

    # From a Pydantic model class (code-defined agents)
    schema = AgentSchema.from_model_class(GeneralAgent)

    # From YAML file
    schema = AgentSchema.from_yaml_file("schema/general.yaml")

    # Programmatic
    schema = AgentSchema.build(
        name="my-agent",
        description="You are a helpful assistant.",
        tools=[{"name": "search", "description": "Query KB"}],
    )

    # Get system prompt (includes tool notes + thinking structure)
    prompt = schema.get_system_prompt()

    # Get pydantic-ai options
    options = schema.get_options()

    # Persist to DB
    Schema(**schema.to_schema_dict())
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, create_model

if TYPE_CHECKING:
    from pydantic_ai import UsageLimits


# =============================================================================
# USAGE LIMITS
# =============================================================================


class AgentUsageLimits(BaseModel):
    """Usage limits for agent runs (maps to pydantic-ai UsageLimits).

    All limits are optional — None means no limit.

    Example YAML::

        limits:
          request_limit: 10
          total_tokens_limit: 50000
    """

    request_limit: int | None = None
    tool_calls_limit: int | None = None
    input_tokens_limit: int | None = None
    output_tokens_limit: int | None = None
    total_tokens_limit: int | None = None

    def to_pydantic_ai(self) -> "UsageLimits":
        """Convert to pydantic-ai UsageLimits for agent.run()."""
        from pydantic_ai import UsageLimits

        return UsageLimits(
            request_limit=self.request_limit,
            tool_calls_limit=self.tool_calls_limit,
            input_tokens_limit=self.input_tokens_limit,
            output_tokens_limit=self.output_tokens_limit,
            total_tokens_limit=self.total_tokens_limit,
        )

    def is_empty(self) -> bool:
        return all(
            v is None
            for v in [
                self.request_limit,
                self.tool_calls_limit,
                self.input_tokens_limit,
                self.output_tokens_limit,
                self.total_tokens_limit,
            ]
        )


# =============================================================================
# TOOL REFERENCE
# =============================================================================


class MCPToolReference(BaseModel):
    """Reference to a tool available to the agent.

    Attributes:
        name: Tool function name (matches MCP tool name)
        server: Server alias (omit or None for local; "rem" = local MCP)
        description: Optional suffix appended to the tool's base description
            from the MCP server. Gives agent-specific context.
    """

    name: str
    server: str | None = None
    description: str | None = None


# =============================================================================
# BACKWARD COMPAT — MCPResourceReference (deprecated, use tools instead)
# =============================================================================


class MCPResourceReference(BaseModel):
    """Deprecated — resources should be listed as tools.

    Kept for backward compatibility when loading old schemas.
    """

    uri: str | None = None
    uri_pattern: str | None = None
    name: str | None = None
    description: str | None = None
    mcp_server: str | None = None


# =============================================================================
# AGENT SCHEMA — Flat, Unified
# =============================================================================


class AgentSchema(BaseModel):
    """Flat schema for declarative agent definition.

    Combines JSON Schema fields (type, description, properties, required)
    with agent config (name, tools, model, limits) at the same level.
    No nested ``json_schema_extra`` wrapper.

    TWO OUTPUT MODES
    ----------------
    **Conversational** (``structured_output: false``, the default):
        Agent returns free-form text.  Properties become "thinking aides"
        — internal scaffolding that guides the LLM's reasoning.

    **Structured** (``structured_output: true``):
        Agent MUST return a Pydantic model matching properties.
        Description is stripped from the schema sent to the LLM.
    """

    # --- JSON Schema standard fields ---
    type: Literal["object"] = "object"
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)

    # --- Agent identity ---
    name: str = ""
    kind: str = "agent"
    version: str = "1.0.0"
    short_description: str | None = None

    # --- Runtime config ---
    structured_output: bool = False
    tools: list[MCPToolReference] = Field(default_factory=list)
    model: str | None = None
    temperature: float | None = None
    max_iterations: int | None = None
    limits: AgentUsageLimits | None = None

    # --- Routing / observation ---
    routing_enabled: bool = True
    routing_max_turns: int = 20
    observation_mode: str = "sync"

    # --- Metadata ---
    tags: list[str] = Field(default_factory=list)
    author: str | None = None
    system_prompt: str | None = None  # extra system prompt appended to description

    model_config = {"extra": "allow", "populate_by_name": True}

    # -----------------------------------------------------------------
    # System prompt (includes tool notes + thinking structure)
    # -----------------------------------------------------------------

    def get_system_prompt(self) -> str:
        """Build the complete system prompt.

        Combines:
        1. description (main system prompt)
        2. system_prompt (optional extension)
        3. Tool notes (tools with description suffixes)
        4. Thinking structure (for unstructured output with properties)
        """
        parts = [self.description]

        if self.system_prompt:
            parts.append(self.system_prompt)

        # Tool notes — description suffixes for agent-specific tool context
        tool_notes = [t for t in self.tools if t.description]
        if tool_notes:
            lines = ["## Tool Notes"]
            for t in tool_notes:
                lines.append(f"- **{t.name}**: {t.description}")
            parts.append("\n".join(lines))

        # Thinking structure for conversational (unstructured) output
        if not self.structured_output:
            guidance = self.to_prompt()
            if guidance:
                parts.append(guidance)

        return "\n\n".join(parts)

    # -----------------------------------------------------------------
    # Runtime options → pydantic-ai Agent kwargs
    # -----------------------------------------------------------------

    def get_options(self, **overrides) -> dict[str, Any]:
        """Get runtime options for pydantic-ai Agent().

        Priority: override > schema > settings default.

        Returns dict with ``model`` and ``model_settings`` keys.
        """
        from p8.settings import get_settings

        s = get_settings()

        model = overrides.get("model") or self.model or s.default_model
        if isinstance(model, str) and ":" not in model:
            model = f"openai:{model}"

        temperature = (
            overrides.get("temperature")
            if "temperature" in overrides
            else self.temperature if self.temperature is not None
            else s.default_temperature
        )

        options: dict[str, Any] = {"model": model}

        model_settings: dict[str, Any] = {}
        if temperature is not None:
            model_settings["temperature"] = temperature
        max_tokens = overrides.get("max_tokens") or s.default_max_tokens
        if max_tokens is not None:
            model_settings["max_tokens"] = max_tokens
        if model_settings:
            options["model_settings"] = model_settings

        return options

    # -----------------------------------------------------------------
    # Output model generation
    # -----------------------------------------------------------------

    def to_output_schema(self, strip_description: bool = True) -> type[BaseModel] | type[str]:  # type: ignore[valid-type]
        """Generate a Pydantic model from schema properties.

        Returns ``str`` if structured_output is disabled or no properties.
        Otherwise creates a dynamic Pydantic model for structured output.
        """
        if not self.structured_output or not self.properties:
            return str  # type: ignore

        # Resolve $refs if present
        defs = self._get_defs()
        resolved_props = {
            k: self._resolve_refs(v, defs)
            for k, v in self.properties.items()
        }

        fields = {}
        for prop_name, prop in resolved_props.items():
            field_type = self._json_type_to_python(prop.get("type", "string"))
            default = ... if prop_name in self.required else None
            fields[prop_name] = (field_type, default)

        base_model = create_model("AgentOutput", **fields)  # type: ignore[call-overload]

        if not strip_description:
            return base_model  # type: ignore[no-any-return]

        class SchemaWrapper(base_model):  # type: ignore
            @classmethod
            def model_json_schema(cls, **kwargs: Any) -> dict[str, Any]:
                schema = super().model_json_schema(**kwargs)
                schema.pop("description", None)
                return schema  # type: ignore[no-any-return]

        SchemaWrapper.__name__ = "AgentOutput"
        return SchemaWrapper  # type: ignore[return-value]

    # -----------------------------------------------------------------
    # Prompt guidance (thinking structure for conversational mode)
    # -----------------------------------------------------------------

    def to_prompt(self) -> str:
        """Convert properties to thinking structure guidance.

        In conversational mode, properties are internal scaffolding —
        the LLM uses them for reasoning but outputs only text.
        """
        if not self.properties:
            return ""

        # Resolve $refs for rendering
        defs = self._get_defs()
        resolved_props = {
            k: self._resolve_refs(v, defs)
            for k, v in self.properties.items()
        }

        lines = [
            "## Thinking Structure",
            "",
            "Use these to guide your reasoning. Do NOT include these labels in output:",
            "",
            "```yaml",
        ]
        lines.extend(self._render_properties_yaml(resolved_props))
        lines.append("```")
        lines.append("")
        lines.append(
            "CRITICAL: Respond with conversational text only. "
            "Do NOT output field names, YAML, or JSON."
        )

        return "\n".join(lines)

    # -----------------------------------------------------------------
    # with_options — create copy with overrides
    # -----------------------------------------------------------------

    def with_options(
        self,
        model: str | None = None,
        temperature: float | None = None,
        max_iterations: int | None = None,
        request_limit: int | None = None,
        tool_calls_limit: int | None = None,
        input_tokens_limit: int | None = None,
        output_tokens_limit: int | None = None,
        total_tokens_limit: int | None = None,
        from_env: bool = False,
        **extra: Any,
    ) -> AgentSchema:
        """Create a copy with overridden options.

        Priority: explicit params > env vars (if from_env) > current values.
        """
        data = self.model_dump(exclude_none=True)

        if from_env:
            env_model = os.getenv("AGENT_MODEL")
            env_temp = os.getenv("AGENT_TEMPERATURE")
            env_iters = os.getenv("AGENT_MAX_ITERATIONS")
            env_req = os.getenv("AGENT_REQUEST_LIMIT")
            env_tok = os.getenv("AGENT_TOTAL_TOKENS_LIMIT")
            env_tc = os.getenv("AGENT_TOOL_CALLS_LIMIT")

            if env_model and not model:
                model = env_model
            if env_temp and temperature is None:
                try:
                    temperature = float(env_temp)
                except ValueError:
                    pass
            if env_iters and max_iterations is None:
                try:
                    max_iterations = int(env_iters)
                except ValueError:
                    pass
            if env_req and request_limit is None:
                try:
                    request_limit = int(env_req)
                except ValueError:
                    pass
            if env_tok and total_tokens_limit is None:
                try:
                    total_tokens_limit = int(env_tok)
                except ValueError:
                    pass
            if env_tc and tool_calls_limit is None:
                try:
                    tool_calls_limit = int(env_tc)
                except ValueError:
                    pass

        if model is not None:
            data["model"] = model
        if temperature is not None:
            data["temperature"] = temperature
        if max_iterations is not None:
            data["max_iterations"] = max_iterations

        limits_specified = any(
            v is not None
            for v in [request_limit, tool_calls_limit, input_tokens_limit,
                      output_tokens_limit, total_tokens_limit]
        )
        if limits_specified:
            existing = data.get("limits") or {}
            if isinstance(existing, AgentUsageLimits):
                existing = existing.model_dump()
            if request_limit is not None:
                existing["request_limit"] = request_limit
            if tool_calls_limit is not None:
                existing["tool_calls_limit"] = tool_calls_limit
            if input_tokens_limit is not None:
                existing["input_tokens_limit"] = input_tokens_limit
            if output_tokens_limit is not None:
                existing["output_tokens_limit"] = output_tokens_limit
            if total_tokens_limit is not None:
                existing["total_tokens_limit"] = total_tokens_limit
            data["limits"] = AgentUsageLimits(**existing)

        data.update(extra)
        return AgentSchema._parse_dict(data)

    # =================================================================
    # CLASS METHODS — Loading from various sources
    # =================================================================

    @classmethod
    def from_yaml(cls, yaml_content: str) -> AgentSchema:
        """Parse agent schema from YAML string."""
        data = yaml.safe_load(yaml_content)
        return cls._parse_dict(data)

    @classmethod
    def from_yaml_file(cls, file_path: str | Path) -> AgentSchema:
        """Load agent schema from a YAML file."""
        content = Path(file_path).read_text()
        return cls.from_yaml(content)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSchema:
        """Create schema from a dictionary."""
        return cls._parse_dict(data)

    @classmethod
    def from_model_class(cls, model_cls: type[BaseModel]) -> "AgentSchema":  # type: ignore[valid-type]
        """Create AgentSchema from a Pydantic model class.

        Uses ``model_json_schema()`` which flattens json_schema_extra
        to the top level.  Parses tool dicts and limits into typed models.
        """
        js = model_cls.model_json_schema()  # type: ignore[attr-defined]
        # Remove Pydantic noise
        js.pop("title", None)
        return cls._parse_dict(js)

    @classmethod
    def from_schema_row(cls, row: Any) -> "AgentSchema":
        """Load AgentSchema from a p8 Schema DB entity.

        Handles three formats:
        - **Flat format** (new): top-level name + tools + properties
        - **Nested format** (old): json_schema_extra wrapper
        - **Legacy format**: json_schema has only config fields (model_name, etc.)
        """
        js = row.json_schema or {}

        # New flat format — name at top level, no json_schema_extra wrapper
        if "name" in js and "json_schema_extra" not in js and "description" in js:
            return cls._parse_dict(js)

        # Old nested format — json_schema_extra contains agent config
        if isinstance(js.get("json_schema_extra"), dict):
            extra = js.pop("json_schema_extra")
            flat = {**js, **extra}
            flat.pop("title", None)
            return cls._parse_dict(flat)

        # Legacy format — json_schema is just config fields
        return cls._from_legacy(js, row)

    @classmethod
    def build(
        cls,
        name: str,
        description: str,
        properties: dict[str, Any] | None = None,
        tools: list[str] | list[MCPToolReference] | list[dict] | None = None,
        version: str = "1.0.0",
        **extra: Any,
    ) -> AgentSchema:
        """Build an agent schema programmatically."""
        tool_refs = _parse_tools(tools or [])

        return cls._parse_dict({
            "type": "object",
            "description": description,
            "properties": properties or {},
            "required": [],
            "name": name,
            "kind": "agent",
            "version": version,
            "tools": tool_refs,
            **extra,
        })

    # =================================================================
    # SERIALIZATION
    # =================================================================

    def to_yaml(self) -> str:
        """Serialize to YAML string."""
        return yaml.dump(  # type: ignore[no-any-return]
            self.model_dump(exclude_none=True),
            default_flow_style=False,
            sort_keys=False,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return self.model_dump(exclude_none=True)

    def to_schema_dict(self, short_description: str | None = None) -> dict[str, Any]:
        """Serialize to a dict for ``Schema(**d)`` in p8.

        Maps AgentSchema fields to p8's Schema entity:
        - name ← self.name
        - kind ← "agent"
        - description ← short_description or truncated system prompt
        - content ← self.description (system prompt, for embedding)
        - json_schema ← full flat AgentSchema dict
        """
        desc = (
            short_description
            or self.short_description
            or self.description[:200]
        )
        return {
            "name": self.name,
            "kind": "agent",
            "description": desc,
            "content": self.description,
            "json_schema": self.model_dump(exclude_none=True),
        }

    # =================================================================
    # PRIVATE HELPERS
    # =================================================================

    @classmethod
    def _parse_dict(cls, data: dict[str, Any]) -> "AgentSchema":
        """Parse a flat dict into AgentSchema, converting nested dicts."""
        data = dict(data)  # shallow copy

        # Parse tools
        raw_tools = data.get("tools", [])
        data["tools"] = _parse_tools(raw_tools)

        # Merge legacy resources into tools
        raw_resources = data.pop("resources", None)
        if raw_resources:
            for r in raw_resources:
                if isinstance(r, dict):
                    name = r.get("name", "").lower().replace(" ", "_")
                    if name and not any(t.name == name for t in data["tools"]):
                        data["tools"].append(MCPToolReference(
                            name=name,
                            description=r.get("description"),
                        ))

        # Parse limits
        if isinstance(data.get("limits"), dict):
            data["limits"] = AgentUsageLimits(**data["limits"])

        # Drop Pydantic schema noise
        data.pop("title", None)
        data.pop("additionalProperties", None)

        return cls(**data)

    @classmethod
    def _from_legacy(cls, js: dict, row: Any) -> "AgentSchema":
        """Reconstruct from legacy format (json_schema = flat config)."""
        tools = _parse_tools(js.get("tools", []))

        # Merge legacy resources
        for r in js.get("resources", []):
            if isinstance(r, dict):
                name = r.get("name", "").lower().replace(" ", "_")
                if name and not any(t.name == name for t in tools):
                    tools.append(MCPToolReference(name=name, description=r.get("description")))

        limits = None
        if isinstance(js.get("limits"), dict):
            limits = AgentUsageLimits(**js["limits"])

        resp_schema = js.get("response_schema") or {}

        return cls(
            type="object",
            description=row.content or row.description or "",
            properties=resp_schema.get("properties", {}),
            required=resp_schema.get("required", []),
            name=row.name,
            tools=tools,
            model=js.get("model_name") or js.get("model"),
            temperature=js.get("temperature"),
            max_iterations=js.get("max_iterations"),
            limits=limits,
            structured_output=js.get("structured_output", False),
            routing_enabled=js.get("routing_enabled", True),
            routing_max_turns=js.get("routing_max_turns", 20),
            observation_mode=js.get("observation_mode", "sync"),
        )

    def _get_defs(self) -> dict[str, Any]:
        """Get JSON Schema $defs for nested model definitions."""
        return (self.model_extra or {}).get("$defs", {})  # type: ignore[no-any-return]

    def _resolve_refs(self, prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
        """Resolve $ref pointers to inline definitions."""
        if not defs:
            return prop

        if "$ref" in prop:
            ref_path = prop["$ref"]  # e.g. "#/$defs/DreamMoment"
            ref_name = ref_path.rsplit("/", 1)[-1]
            if ref_name in defs:
                return defs[ref_name]  # type: ignore[no-any-return]
            return prop

        # Resolve nested $refs in items (arrays)
        if "items" in prop and isinstance(prop["items"], dict):
            resolved_items = self._resolve_refs(prop["items"], defs)
            prop = {**prop, "items": resolved_items}

        # Resolve nested $refs in properties (objects)
        if "properties" in prop and isinstance(prop["properties"], dict):
            resolved = {}
            for k, v in prop["properties"].items():
                resolved[k] = self._resolve_refs(v, defs) if isinstance(v, dict) else v
            prop = {**prop, "properties": resolved}

        return prop

    @staticmethod
    def _json_type_to_python(json_type: str) -> type:  # type: ignore[valid-type]
        type_map: dict[str, type] = {
            "string": str,
            "number": float,
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        return type_map.get(json_type, str)

    def _render_properties_yaml(
        self, properties: dict[str, Any], indent: int = 0,
    ) -> list[str]:
        """Render properties as YAML-like text for prompt guidance."""
        lines: list[str] = []
        prefix = "  " * indent

        for field_name, field_def in properties.items():
            field_type = field_def.get("type", "any")
            field_desc = field_def.get("description", "")
            is_required = field_name in self.required

            if field_type == "object":
                lines.append(f"{prefix}{field_name}:")
                if field_desc:
                    lines.append(f"{prefix}  # {field_desc}")
                nested = field_def.get("properties", {})
                if nested:
                    lines.extend(self._render_properties_yaml(nested, indent + 1))

            elif field_type == "array":
                items = field_def.get("items", {})
                items_type = items.get("type", "any")
                lines.append(f"{prefix}{field_name}: [{items_type}]")
                if field_desc:
                    lines.append(f"{prefix}  # {field_desc}")
                if items_type == "object":
                    nested = items.get("properties", {})
                    if nested:
                        lines.append(f"{prefix}  # Each item:")
                        lines.extend(self._render_properties_yaml(nested, indent + 2))

            else:
                enum_vals = field_def.get("enum")
                type_str = (
                    f"{field_type} (one of: {', '.join(str(v) for v in enum_vals)})"
                    if enum_vals else field_type
                )
                req = " (required)" if is_required else ""
                lines.append(f"{prefix}{field_name}: {type_str}{req}")
                if field_desc:
                    lines.append(f"{prefix}  # {field_desc}")

        return lines


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def _parse_tools(raw: list) -> list[MCPToolReference]:
    """Parse a list of tool dicts/strings into MCPToolReference objects."""
    tools: list[MCPToolReference] = []
    for t in raw:
        if isinstance(t, MCPToolReference):
            tools.append(t)
        elif isinstance(t, dict):
            tools.append(MCPToolReference(**t))
        elif isinstance(t, str):
            tools.append(MCPToolReference(name=t))
    return tools


def get_system_prompt(schema: AgentSchema | dict[str, Any]) -> str:
    """Extract system prompt from schema (polymorphic version)."""
    if isinstance(schema, AgentSchema):
        return schema.get_system_prompt()

    base: str = schema.get("description", "")
    custom = schema.get("system_prompt")
    if custom:
        return f"{base}\n\n{custom}"
    return base


# =============================================================================
# BACKWARD COMPAT — AgentConfig alias
# =============================================================================

# AgentConfig was the nested config wrapper in the old design.
# Code that imports AgentConfig now gets AgentSchema (flat).
# Legacy code that builds AgentConfig directly should migrate to AgentSchema.
AgentConfig = AgentSchema
