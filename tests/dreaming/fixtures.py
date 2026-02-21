"""Shared fixture data for dreaming tests."""

from __future__ import annotations

from uuid import UUID

from p8.ontology.types import Moment, Resource, Session
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.tokens import estimate_tokens

TEST_USER_ID = UUID("dddddddd-0000-0000-0000-000000000001")

# Entity names (deterministic IDs derive from these)
SESSION_ML = "test-dream-session-ml"
SESSION_ARCH = "test-dream-session-arch"
MOMENT_ML = "session-ml-chunk-0"
MOMENT_ARCH = "session-arch-chunk-0"
RESOURCE_ML = "ml-report-chunk-0000"
RESOURCE_ARCH = "arch-doc-chunk-0000"


async def setup_dreaming_fixtures(db, encryption):
    """Create sessions, messages, moments, and resources for dreaming tests.

    Returns (session_a, session_b, moment_a, moment_b, resource_a, resource_b).
    """
    session_repo = Repository(Session, db, encryption)
    moment_repo = Repository(Moment, db, encryption)
    resource_repo = Repository(Resource, db, encryption)
    memory = MemoryService(db, encryption)

    # Sessions
    session_a = Session(name=SESSION_ML, mode="chat", user_id=TEST_USER_ID)
    session_b = Session(name=SESSION_ARCH, mode="chat", user_id=TEST_USER_ID)
    [sa, sb] = await session_repo.upsert([session_a, session_b])

    # Messages for session A (ML topics)
    ml_messages = [
        ("user", "I want to build a training pipeline for our ML models"),
        ("assistant", "Great idea. You should consider using a DAG orchestrator like Airflow."),
        ("user", "What about data preprocessing? We have messy CSV files."),
        ("assistant", "For CSV cleaning I recommend pandas with schema validation via pandera."),
        ("user", "How should we handle feature engineering?"),
        ("assistant", "Feature stores like Feast or Tecton centralize feature computation and serving."),
    ]
    for mtype, content in ml_messages:
        await memory.persist_message(
            sa.id, mtype, content, user_id=TEST_USER_ID, token_count=estimate_tokens(content),
        )

    # Messages for session B (architecture topics)
    arch_messages = [
        ("user", "We need to decide on our API gateway pattern."),
        ("assistant", "Consider Kong or Envoy for a production API gateway."),
        ("user", "What about microservice communication?"),
        ("assistant", "For sync use gRPC, for async use a message queue like RabbitMQ or NATS."),
    ]
    for mtype, content in arch_messages:
        await memory.persist_message(
            sb.id, mtype, content, user_id=TEST_USER_ID, token_count=estimate_tokens(content),
        )

    # Moments
    moment_a = Moment(
        name=MOMENT_ML,
        moment_type="session_chunk",
        summary=(
            "Discussed ML model training pipelines, data preprocessing with pandas, "
            "and feature engineering using feature stores like Feast."
        ),
        source_session_id=sa.id,
        user_id=TEST_USER_ID,
        topic_tags=["machine-learning", "data-pipeline", "feature-engineering"],
        graph_edges=[{"target": RESOURCE_ML, "relation": "references", "weight": 0.8}],
        metadata={"message_count": 6, "token_count": 300, "chunk_index": 0},
    )
    moment_b = Moment(
        name=MOMENT_ARCH,
        moment_type="session_chunk",
        summary=(
            "Discussed microservices architecture, API gateway patterns with Kong/Envoy, "
            "and inter-service communication using gRPC and message queues."
        ),
        source_session_id=sb.id,
        user_id=TEST_USER_ID,
        topic_tags=["architecture", "microservices", "api-gateway"],
        graph_edges=[{"target": RESOURCE_ARCH, "relation": "references", "weight": 0.7}],
        metadata={"message_count": 4, "token_count": 200, "chunk_index": 0},
    )
    [ma, mb] = await moment_repo.upsert([moment_a, moment_b])

    # Resources
    resource_a = Resource(
        name=RESOURCE_ML,
        content=(
            "ML Training Pipeline Report: Our current pipeline processes 10M records daily. "
            "Key bottlenecks include feature computation (40% of wall time) and data validation. "
            "Recommended improvements: migrate to a feature store, add schema validation, "
            "and implement incremental training for reduced compute costs."
        ),
        category="document",
        user_id=TEST_USER_ID,
    )
    resource_b = Resource(
        name=RESOURCE_ARCH,
        content=(
            "Architecture Decision Record: API Gateway. Decision: use Kong with rate limiting "
            "and JWT validation at the edge. Services communicate via gRPC for sync calls "
            "and NATS JetStream for async events. Circuit breakers via Envoy sidecar."
        ),
        category="document",
        user_id=TEST_USER_ID,
    )
    [ra, rb] = await resource_repo.upsert([resource_a, resource_b])

    return sa, sb, ma, mb, ra, rb
