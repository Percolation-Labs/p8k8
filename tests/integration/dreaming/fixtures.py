"""Shared fixture data for dreaming tests.

Provides thematically diverse sessions to test moment grouping:
  - Technical: ML pipelines + microservices architecture (should group into 1 dream)
  - Personal: trail running + nature journaling (should group into 1 dream)
  - Health: sleep tracking + nutrition (distinct from the above)
"""

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
SESSION_TRAIL = "test-dream-session-trail"
SESSION_SLEEP = "test-dream-session-sleep"
MOMENT_ML = "session-ml-chunk-0"
MOMENT_ARCH = "session-arch-chunk-0"
MOMENT_TRAIL = "session-trail-chunk-0"
MOMENT_SLEEP = "session-sleep-chunk-0"
RESOURCE_ML = "ml-report-chunk-0000"
RESOURCE_ARCH = "arch-doc-chunk-0000"
RESOURCE_TRAIL = "trail-journal-chunk-0000"
RESOURCE_SLEEP = "sleep-log-chunk-0000"


async def setup_dreaming_fixtures(db, encryption):
    """Create sessions, messages, moments, and resources for dreaming tests.

    Four sessions across three distinct themes:
      - Technical (ML + architecture) -- related, should merge
      - Outdoor/personal (trail running + nature journaling) -- related, should merge
      - Health (sleep + nutrition) -- distinct from the above

    Returns (session_a, session_b, moment_a, moment_b, resource_a, resource_b).
    Original 2-tuple return maintained for backward compat; additional fixtures
    are still in the DB for the dreamer to discover.
    """
    session_repo = Repository(Session, db, encryption)
    moment_repo = Repository(Moment, db, encryption)
    resource_repo = Repository(Resource, db, encryption)
    memory = MemoryService(db, encryption)

    # ── Sessions ──────────────────────────────────────────────
    session_a = Session(name=SESSION_ML, mode="chat", user_id=TEST_USER_ID)
    session_b = Session(name=SESSION_ARCH, mode="chat", user_id=TEST_USER_ID)
    session_c = Session(name=SESSION_TRAIL, mode="chat", user_id=TEST_USER_ID)
    session_d = Session(name=SESSION_SLEEP, mode="chat", user_id=TEST_USER_ID)
    [sa, sb, sc, sd] = await session_repo.upsert([session_a, session_b, session_c, session_d])

    # ── Messages: Session A (ML topics) ───────────────────────
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

    # ── Messages: Session B (architecture topics) ─────────────
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

    # ── Messages: Session C (trail running + nature) ──────────
    trail_messages = [
        ("user", "I ran the Rattlesnake Ledge trail yesterday. 11 miles, 2400ft elevation."),
        ("assistant", "That's a solid climb. How did your legs feel on the descent?"),
        ("user", "Quads were sore but manageable. I spotted a pileated woodpecker near the ridge."),
        ("assistant", "Nice sighting! Those are uncommon at that elevation. Did you log it on iNaturalist?"),
        ("user", "Not yet. I also found a patch of chanterelles but it's too early to pick."),
        ("assistant", "Good call waiting. Late September is usually peak for chanterelles in the PNW."),
    ]
    for mtype, content in trail_messages:
        await memory.persist_message(
            sc.id, mtype, content, user_id=TEST_USER_ID, token_count=estimate_tokens(content),
        )

    # ── Messages: Session D (sleep and nutrition) ─────────────
    sleep_messages = [
        ("user", "My sleep has been terrible. Averaging 5.5 hours according to my tracker."),
        ("assistant", "That's well below the recommended 7-9 hours. What time are you going to bed?"),
        ("user", "Usually around midnight but I'm on screens until then."),
        ("assistant", "Blue light suppresses melatonin. Try a 30-min screen-free wind-down before bed."),
        ("user", "I've also been skipping breakfast and just having coffee until noon."),
        ("assistant", "Intermittent fasting can work but combined with poor sleep it spikes cortisol. Consider a light breakfast with protein."),
    ]
    for mtype, content in sleep_messages:
        await memory.persist_message(
            sd.id, mtype, content, user_id=TEST_USER_ID, token_count=estimate_tokens(content),
        )

    # ── Moments ───────────────────────────────────────────────
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
    moment_c = Moment(
        name=MOMENT_TRAIL,
        moment_type="session_chunk",
        summary=(
            "Ran Rattlesnake Ledge trail (11mi, 2400ft). Spotted a pileated woodpecker "
            "near the ridge and found early chanterelles. Quads sore on descent."
        ),
        source_session_id=sc.id,
        user_id=TEST_USER_ID,
        topic_tags=["trail-running", "birdwatching", "mushroom-foraging", "pacific-northwest"],
        graph_edges=[{"target": RESOURCE_TRAIL, "relation": "references", "weight": 0.8}],
        metadata={"message_count": 6, "token_count": 280, "chunk_index": 0},
    )
    moment_d = Moment(
        name=MOMENT_SLEEP,
        moment_type="session_chunk",
        summary=(
            "Sleep averaging 5.5 hours, staying on screens until midnight. Skipping "
            "breakfast and relying on coffee until noon. Discussed blue light, melatonin "
            "suppression, and cortisol impact of combining poor sleep with fasting."
        ),
        source_session_id=sd.id,
        user_id=TEST_USER_ID,
        topic_tags=["sleep", "nutrition", "health", "cortisol"],
        graph_edges=[{"target": RESOURCE_SLEEP, "relation": "references", "weight": 0.7}],
        metadata={"message_count": 6, "token_count": 260, "chunk_index": 0},
    )
    [ma, mb, mc, md] = await moment_repo.upsert([moment_a, moment_b, moment_c, moment_d])

    # ── Resources ─────────────────────────────────────────────
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
    resource_c = Resource(
        name=RESOURCE_TRAIL,
        content=(
            "Trail Journal — Rattlesnake Ledge, Feb 2026. Distance: 11.2mi, elevation gain: "
            "2,430ft. Conditions: clear, 42F at trailhead. Wildlife: pileated woodpecker "
            "(Dryocopus pileatus) at 3,100ft, red-tailed hawk soaring over the lake. "
            "Fungi: early Cantharellus formosus (Pacific golden chanterelle) along the "
            "north-facing slope — too small to harvest, check back late September."
        ),
        category="journal",
        user_id=TEST_USER_ID,
    )
    resource_d = Resource(
        name=RESOURCE_SLEEP,
        content=(
            "Sleep Tracker Summary — Feb 2026. Average sleep: 5h 32m (target: 7h 30m). "
            "Average bedtime: 00:14. Screen time before bed: 2h 10m average. "
            "REM sleep: 48min (below 90min target). Deep sleep: 35min (below 60min target). "
            "Notes: caffeine intake averaging 3 cups before noon, no food until 12:30pm."
        ),
        category="health",
        user_id=TEST_USER_ID,
    )
    [ra, rb, rc, rd] = await resource_repo.upsert([resource_a, resource_b, resource_c, resource_d])

    # Content upload moments — link resources to sessions so Phase 1 enrichment fires
    upload_ml = Moment(
        name="upload-ml-report",
        moment_type="content_upload",
        summary=f"Uploaded ml-report.pdf (1 chunks, 280 chars).\nResources: {RESOURCE_ML}",
        source_session_id=sa.id,
        user_id=TEST_USER_ID,
        metadata={
            "source": "upload",
            "file_name": "ml-report.pdf",
            "resource_keys": [RESOURCE_ML],
        },
    )
    upload_arch = Moment(
        name="upload-arch-doc",
        moment_type="content_upload",
        summary=f"Uploaded arch-doc.pdf (1 chunks, 250 chars).\nResources: {RESOURCE_ARCH}",
        source_session_id=sb.id,
        user_id=TEST_USER_ID,
        metadata={
            "source": "upload",
            "file_name": "arch-doc.pdf",
            "resource_keys": [RESOURCE_ARCH],
        },
    )
    await moment_repo.upsert([upload_ml, upload_arch])

    # Return original tuple shape for backward compat
    return sa, sb, ma, mb, ra, rb
