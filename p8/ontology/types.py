"""Built-in ontology types — all entity models that map to database tables.

Each model defines:
  __table_name__       — postgres table name
  __id_fields__        — ordered fields for deterministic ID (first non-None wins; () → uuid4)
  __embedding_field__  — which field to embed (None to disable)
  __encrypted_fields__ — fields encrypted at rest {field: "randomized"|"deterministic"}
  __redacted_fields__  — fields that pass through PII pipeline before storage/embedding

Helper models (GraphEdge, ToolReference, ResourceReference) are embedded
in JSONB columns, not standalone tables.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from p8.ontology.base import CoreModel


# ---------------------------------------------------------------------------
# Helper models (embedded in JSONB, not standalone tables)
# ---------------------------------------------------------------------------


class GraphEdge(BaseModel):
    """A directed edge in the pseudo-graph. Stored in graph_edges JSONB."""

    target: str
    relation: str = "related"
    weight: float = 1.0


class ToolReference(BaseModel):
    """Pointer to a remote tool on a server. Embedded in schema json_schema."""

    name: str
    server: str
    protocol: str = "mcp"
    description: str | None = None


class ResourceReference(BaseModel):
    """Pointer to an MCP resource for context injection."""

    uri: str
    name: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Entity models (each maps to a database table)
# ---------------------------------------------------------------------------


class Schema(CoreModel):
    """The ontology registry. Models, agents, evaluators, tools — everything
    is a schema row. For agents: content = system prompt, json_schema = spec."""

    __table_name__ = "schemas"
    __embedding_field__ = "description"
    __encrypted_fields__ = {}
    __redacted_fields__ = []

    name: str
    kind: str = "model"  # model | agent | evaluator | tool | resource | moment
    version: str | None = None
    description: str | None = None
    content: str | None = None  # YAML source / system prompt
    json_schema: dict | None = None  # compiled spec (tools, model, response_schema, etc.)


class Ontology(CoreModel):
    """Domain knowledge entities — wiki pages, parsed documents, extracted data.
    Small pages (<500 tokens) with [key](path) markdown links forming a knowledge graph."""

    __table_name__ = "ontologies"
    __id_fields__ = ("uri", "name")
    __embedding_field__ = "content"
    __encrypted_fields__ = {"content": "randomized"}
    __redacted_fields__ = ["content"]

    name: str
    uri: str | None = None
    content: str | None = None
    extracted_data: dict | None = None  # structured output from ontology-type agents
    file_id: UUID | None = None
    agent_schema_id: UUID | None = None  # which agent schema parsed this
    confidence_score: float | None = None


class Resource(CoreModel):
    """Documents, chunks, artifacts. Ordered by ordinal within a parent."""

    __table_name__ = "resources"
    __id_fields__ = ("uri", "name")
    __embedding_field__ = "content"
    __encrypted_fields__ = {"content": "randomized"}
    __redacted_fields__ = ["content"]

    name: str
    uri: str | None = None
    ordinal: int | None = None
    content: str | None = None
    category: str | None = None
    related_entities: list[str] = Field(default_factory=list)


class Moment(CoreModel):
    """Temporal events — session chunks, meetings, observations, uploads.
    Chain backwards via previous_moment_keys for history traversal.
    Summaries are generated via privacy-aware summarization (Clio pattern)."""

    __table_name__ = "moments"
    __embedding_field__ = "summary"
    __encrypted_fields__ = {"summary": "randomized"}
    __redacted_fields__ = ["summary"]

    name: str
    moment_type: str | None = None  # session_chunk | meeting | observation | content_upload
    summary: str | None = None
    image_uri: str | None = None
    starts_timestamp: datetime | None = None
    ends_timestamp: datetime | None = None
    present_persons: list[dict] = Field(default_factory=list)
    emotion_tags: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    category: str | None = None
    source_session_id: UUID | None = None
    previous_moment_keys: list[str] = Field(default_factory=list)


class Session(CoreModel):
    """Conversation state. Routes to an agent via agent_name.
    Routing table lives in metadata JSONB."""

    __table_name__ = "sessions"
    __id_fields__ = ()
    __embedding_field__ = "description"
    __encrypted_fields__ = {}
    __redacted_fields__ = []

    name: str | None = None
    description: str | None = None
    agent_name: str | None = None
    mode: str | None = None  # chat | workflow | eval
    total_tokens: int = 0


class Message(CoreModel):
    """Chat history. token_count populated at persist time for budget math.
    tool_calls stores structured tool invocation metadata.
    Content is encrypted and redacted — highest sensitivity."""

    __table_name__ = "messages"
    __id_fields__ = ()
    __embedding_field__ = "content"
    __encrypted_fields__ = {"content": "randomized"}
    __redacted_fields__ = ["content"]

    session_id: UUID
    message_type: str = "user"  # user | assistant | system | tool_call | tool_result | observation | memory | think
    content: str | None = None
    token_count: int = 0
    tool_calls: dict | None = None
    trace_id: str | None = None
    span_id: str | None = None


class Server(CoreModel):
    """Remote tool server registry. MCP (Streamable HTTP) or OpenAPI."""

    __table_name__ = "servers"
    __embedding_field__ = None
    __encrypted_fields__ = {}
    __redacted_fields__ = []

    name: str
    url: str | None = None
    protocol: str = "mcp"  # mcp | openapi
    auth_config: dict = Field(default_factory=dict)
    enabled: bool = True
    description: str | None = None


class Tool(CoreModel):
    """Registered tool definition discovered from a server."""

    __table_name__ = "tools"
    __embedding_field__ = "description"
    __encrypted_fields__ = {}
    __redacted_fields__ = []

    name: str
    server_id: UUID | None = None
    description: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    enabled: bool = True


class User(CoreModel):
    """User profile. content field holds free-text bio for embedding.
    Auth provider metadata stored in metadata JSONB.
    Email uses deterministic encryption for exact-match lookup."""

    __table_name__ = "users"
    __id_fields__ = ("email",)
    __embedding_field__ = "content"
    __encrypted_fields__ = {"content": "randomized", "email": "deterministic"}
    __redacted_fields__ = ["content"]

    name: str
    email: str | None = None
    interests: list[str] = Field(default_factory=list)
    activity_level: str | None = None
    content: str | None = None


class File(CoreModel):
    """Uploaded/parsed document. parsed_content is extracted text,
    parsed_output is structured parse result (e.g. from ontology-type agent)."""

    __table_name__ = "files"
    __id_fields__ = ("uri", "name")
    __embedding_field__ = "parsed_content"
    __encrypted_fields__ = {"parsed_content": "randomized"}
    __redacted_fields__ = ["parsed_content"]

    name: str
    uri: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    parsed_content: str | None = None
    parsed_output: dict | None = None


class Feedback(CoreModel):
    """User ratings on agent responses. Linked to session + message."""

    __table_name__ = "feedback"
    __id_fields__ = ()
    __embedding_field__ = None
    __encrypted_fields__ = {"comment": "randomized"}
    __redacted_fields__ = ["comment"]

    session_id: UUID | None = None
    message_id: UUID | None = None
    rating: int | None = None
    comment: str | None = None
    trace_id: str | None = None
    span_id: str | None = None


class StorageGrant(CoreModel):
    """Cloud storage folder sync permission. Tracks which folders
    a user has granted access to (Google Drive, iCloud)."""

    __table_name__ = "storage_grants"
    __id_fields__ = ()
    __embedding_field__ = None
    __encrypted_fields__ = {}
    __redacted_fields__ = []

    user_id_ref: UUID  # FK to users.id (named to avoid shadowing CoreModel.user_id)
    provider: str  # google-drive | icloud
    provider_folder_id: str | None = None
    folder_name: str | None = None
    folder_path: str | None = None
    sync_mode: str = "incremental"  # incremental | full
    auto_sync: bool = True
    last_sync_at: datetime | None = None
    sync_cursor: str | None = None  # provider-specific change token
    status: str = "active"  # active | paused | revoked


class Tenant(CoreModel):
    """Tenant entity — owns users, encryption keys, and all scoped data.
    Encryption mode determines how data-at-rest is handled for this tenant."""

    __table_name__ = "tenants"
    __embedding_field__ = None
    __encrypted_fields__ = {}
    __redacted_fields__ = []

    name: str
    encryption_mode: str = "platform"  # platform | client | sealed | disabled
    status: str = "active"  # active | suspended | deleted


# ---------------------------------------------------------------------------
# Registry of all entity types for codegen / iteration
# ---------------------------------------------------------------------------

ALL_ENTITY_TYPES: list[type[CoreModel]] = [
    Schema,
    Ontology,
    Resource,
    Moment,
    Session,
    Message,
    Server,
    Tool,
    User,
    File,
    Feedback,
    StorageGrant,
    Tenant,
]

# Tables that get companion embeddings tables
EMBEDDABLE_TABLES: list[str] = [
    t.__table_name__
    for t in ALL_ENTITY_TYPES
    if getattr(t, "__embedding_field__", None) is not None
]

# Tables that participate in KV store (have a name column for key resolution)
KV_TABLES: list[str] = [
    "schemas",
    "ontologies",
    "resources",
    "moments",
    "sessions",
    "servers",
    "tools",
    "users",
    "files",
    "tenants",
]

# Tables with encrypted fields (content stored as ciphertext)
ENCRYPTED_TABLES: list[str] = [
    t.__table_name__
    for t in ALL_ENTITY_TYPES
    if getattr(t, "__encrypted_fields__", {})
]

# Tables with redacted fields (PII pipeline before storage/embedding)
REDACTED_TABLES: list[str] = [
    t.__table_name__
    for t in ALL_ENTITY_TYPES
    if getattr(t, "__redacted_fields__", [])
]

# Canonical table name → model class lookup (used by routers, CLI, etc.)
TABLE_MAP: dict[str, type[CoreModel]] = {t.__table_name__: t for t in ALL_ENTITY_TYPES}
