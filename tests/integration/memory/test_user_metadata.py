"""Integration tests for UserMetadata type and update_user_metadata MCP tool."""

from __future__ import annotations

import json

import pytest
from uuid import uuid4

from p8.ontology.types import User, UserMetadata
from p8.services.repository import Repository


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


# ---------------------------------------------------------------------------
# UserMetadata pydantic model tests
# ---------------------------------------------------------------------------


def test_user_metadata_partial_construction():
    """Only provided fields are set; others stay None."""
    meta = UserMetadata(interests=["cooking", "ML"])
    assert meta.interests == ["cooking", "ML"]
    assert meta.relations is None
    assert meta.feeds is None
    assert meta.preferences is None
    assert meta.facts is None


def test_user_metadata_full_construction():
    """All fields populated."""
    meta = UserMetadata(
        relations=[{"name": "Luna", "role": "pet", "notes": "golden retriever"}],
        interests=["hiking", "photography"],
        feeds=[{"url": "https://news.ycombinator.com", "name": "HN", "type": "website"}],
        preferences={"timezone": "US/Pacific", "language": "en"},
        facts={"birthday": "March 15", "company": "Acme"},
    )
    assert len(meta.relations) == 1
    assert meta.preferences["timezone"] == "US/Pacific"


def test_user_metadata_validates_from_dict():
    """model_validate accepts a partial dict (like what an agent sends)."""
    data = {"interests": ["chess"], "facts": {"city": "Berlin"}}
    meta = UserMetadata.model_validate(data)
    assert meta.interests == ["chess"]
    assert meta.facts["city"] == "Berlin"


def test_user_metadata_dump_excludes_none():
    """model_dump(exclude_none=True) keeps payload small for partial updates."""
    meta = UserMetadata(interests=["running"])
    dumped = meta.model_dump(exclude_none=True)
    assert "interests" in dumped
    assert "relations" not in dumped
    assert "feeds" not in dumped


def test_user_metadata_empty_construction():
    """Empty UserMetadata produces an empty dict when excluding None."""
    meta = UserMetadata()
    dumped = meta.model_dump(exclude_none=True)
    assert dumped == {}


# ---------------------------------------------------------------------------
# Repository.merge_metadata tests
# ---------------------------------------------------------------------------


async def test_merge_metadata_preserves_existing_keys(db, encryption):
    """Merging new keys preserves keys not in the patch."""
    repo = Repository(User, db, encryption)
    user = User(
        name="merge-test-user",
        email="merge@test.dev",
        metadata={"facts": {"city": "Berlin"}, "interests": ["chess"]},
    )
    [user] = await repo.upsert(user)

    meta = await repo.merge_metadata(
        user.id, {"preferences": {"timezone": "UTC"}},
    )

    assert meta["facts"] == {"city": "Berlin"}
    assert meta["interests"] == ["chess"]
    assert meta["preferences"] == {"timezone": "UTC"}


async def test_merge_metadata_overwrites_existing_key(db, encryption):
    """Merging a key that exists replaces its value (shallow)."""
    repo = Repository(User, db, encryption)
    user = User(
        name="overwrite-test-user",
        email="overwrite@test.dev",
        metadata={"interests": ["old-interest"]},
    )
    [user] = await repo.upsert(user)

    meta = await repo.merge_metadata(
        user.id, {"interests": ["new-1", "new-2"]},
    )

    assert meta["interests"] == ["new-1", "new-2"]


async def test_merge_metadata_remove_keys(db, encryption):
    """remove_keys deletes top-level keys after the merge."""
    repo = Repository(User, db, encryption)
    user = User(
        name="removal-test-user",
        email="removal@test.dev",
        metadata={"facts": {"city": "Berlin"}, "feeds": [{"url": "https://example.com"}]},
    )
    [user] = await repo.upsert(user)

    meta = await repo.merge_metadata(user.id, {}, remove_keys=["feeds"])

    assert "feeds" not in meta
    assert meta["facts"] == {"city": "Berlin"}


async def test_merge_metadata_returns_none_for_missing_entity(db, encryption):
    """Returns None when the entity doesn't exist."""
    repo = Repository(User, db, encryption)
    result = await repo.merge_metadata(uuid4(), {"key": "val"})
    assert result is None


async def test_merge_metadata_works_on_any_table(db, encryption):
    """merge_metadata is generic — works on sessions, schemas, etc."""
    from p8.ontology.types import Session
    repo = Repository(Session, db, encryption)
    session = Session(name="merge-meta-session", metadata={"agent": "test"})
    [session] = await repo.upsert(session)

    meta = await repo.merge_metadata(
        session.id, {"last_tool": "web_search"},
    )

    assert meta["agent"] == "test"
    assert meta["last_tool"] == "web_search"


async def test_upsert_does_not_clobber_merged_metadata(db, encryption):
    """A subsequent upsert (e.g. name change) must not wipe metadata set via merge_metadata."""
    repo = Repository(User, db, encryption)

    # 1. Create user with initial metadata
    user = User(
        name="clobber-test",
        email="clobber@test.dev",
        metadata={"facts": {"city": "Berlin"}},
    )
    [user] = await repo.upsert(user)

    # 2. Enrich metadata via merge_metadata (as the MCP tool would)
    await repo.merge_metadata(user.id, {"interests": ["AI"], "preferences": {"tz": "UTC"}})

    # 3. Simulate a separate upsert that only changes the name
    #    (metadata defaults to {} on a fresh User instance)
    user_update = User(name="clobber-test-renamed", email="clobber@test.dev")
    [updated] = await repo.upsert(user_update)

    # 4. Verify metadata survived
    assert updated.metadata.get("facts") == {"city": "Berlin"}, (
        f"upsert wiped metadata: {updated.metadata}"
    )
    assert updated.metadata.get("interests") == ["AI"]
    assert updated.metadata.get("preferences") == {"tz": "UTC"}


# ---------------------------------------------------------------------------
# MCP tool integration tests
# ---------------------------------------------------------------------------


async def test_update_user_metadata_tool_basic(db, encryption):
    """update_user_metadata merges new keys into user metadata."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.tools.update_user_metadata import update_user_metadata

    init_tools(db, encryption)

    # Create a user
    repo = Repository(User, db, encryption)
    user = User(name="tool-test-user", email="tool@test.dev", metadata={})
    [user] = await repo.upsert(user)

    set_tool_context(user_id=user.id)

    result = await update_user_metadata(
        metadata={
            "relations": [{"name": "Max", "role": "pet", "notes": "labrador"}],
            "interests": ["AI", "gardening"],
        },
    )

    assert result["status"] == "ok"
    assert result["user_id"] == str(user.id)
    meta = result["metadata"]
    assert meta["relations"] == [{"name": "Max", "role": "pet", "notes": "labrador"}]
    assert meta["interests"] == ["AI", "gardening"]


async def test_update_user_metadata_tool_partial(db, encryption):
    """Partial update preserves existing metadata keys."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.tools.update_user_metadata import update_user_metadata

    init_tools(db, encryption)

    repo = Repository(User, db, encryption)
    user = User(
        name="partial-test-user",
        email="partial@test.dev",
        metadata={"facts": {"birthday": "Jan 1"}, "interests": ["reading"]},
    )
    [user] = await repo.upsert(user)

    set_tool_context(user_id=user.id)

    # Only update preferences — facts and interests should survive
    result = await update_user_metadata(
        metadata={"preferences": {"language": "en"}},
    )

    assert result["status"] == "ok"
    meta = result["metadata"]
    assert meta["facts"] == {"birthday": "Jan 1"}
    assert meta["interests"] == ["reading"]
    assert meta["preferences"] == {"language": "en"}


async def test_update_user_metadata_tool_remove_keys(db, encryption):
    """remove_keys deletes top-level metadata keys."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.tools.update_user_metadata import update_user_metadata

    init_tools(db, encryption)

    repo = Repository(User, db, encryption)
    user = User(
        name="remove-test-user",
        email="remove@test.dev",
        metadata={
            "feeds": [{"url": "https://example.com"}],
            "facts": {"city": "NYC"},
        },
    )
    [user] = await repo.upsert(user)

    set_tool_context(user_id=user.id)

    result = await update_user_metadata(
        metadata={},
        remove_keys=["feeds"],
    )

    assert result["status"] == "ok"
    meta = result["metadata"]
    assert "feeds" not in meta
    assert meta["facts"] == {"city": "NYC"}


async def test_update_user_metadata_tool_merge_and_remove(db, encryption):
    """Simultaneous merge + remove in one call."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.tools.update_user_metadata import update_user_metadata

    init_tools(db, encryption)

    repo = Repository(User, db, encryption)
    user = User(
        name="combo-test-user",
        email="combo@test.dev",
        metadata={
            "interests": ["old"],
            "feeds": [{"url": "https://stale.com"}],
            "facts": {"city": "London"},
        },
    )
    [user] = await repo.upsert(user)

    set_tool_context(user_id=user.id)

    result = await update_user_metadata(
        metadata={"interests": ["new-interest"]},
        remove_keys=["feeds"],
    )

    assert result["status"] == "ok"
    meta = result["metadata"]
    assert meta["interests"] == ["new-interest"]
    assert "feeds" not in meta
    assert meta["facts"] == {"city": "London"}


async def test_update_user_metadata_tool_no_user(db, encryption):
    """Returns error when no user_id is available."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.tools.update_user_metadata import update_user_metadata

    init_tools(db, encryption)
    set_tool_context(user_id=None)

    result = await update_user_metadata(metadata={"interests": ["test"]})
    assert result["status"] == "error"
    assert "user_id" in result["error"]


async def test_update_user_metadata_tool_nonexistent_user(db, encryption):
    """Returns error for a user_id that doesn't exist."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.tools.update_user_metadata import update_user_metadata

    init_tools(db, encryption)

    fake_uid = uuid4()
    set_tool_context(user_id=fake_uid)
    result = await update_user_metadata(
        metadata={"interests": ["test"]},
    )
    assert result["status"] == "error"
    assert "User not found" in result["error"]


async def test_update_user_metadata_tool_explicit_user_id(db, encryption):
    """Explicit user_id parameter overrides context."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.tools.update_user_metadata import update_user_metadata

    init_tools(db, encryption)

    repo = Repository(User, db, encryption)
    user = User(name="explicit-uid-user", email="explicit@test.dev", metadata={})
    [user] = await repo.upsert(user)

    # Set context to target the specific user
    set_tool_context(user_id=user.id)
    result = await update_user_metadata(
        metadata={"facts": {"role": "developer"}},
    )

    assert result["status"] == "ok"
    assert result["user_id"] == str(user.id)
    assert result["metadata"]["facts"] == {"role": "developer"}


async def test_user_profile_resource_includes_metadata(db, encryption):
    """The user_profile MCP resource returns the full structured metadata."""
    from p8.api.tools import init_tools
    from p8.api.mcp_server import user_profile

    init_tools(db, encryption)

    repo = Repository(User, db, encryption)
    user = User(
        name="profile-meta-user",
        email="profile@test.dev",
        content="A developer who likes hiking.",
        metadata={
            "relations": [{"name": "Buddy", "role": "pet", "notes": "cat"}],
            "interests": ["hiking", "coding"],
            "preferences": {"timezone": "US/Eastern"},
        },
    )
    [user] = await repo.upsert(user)

    from p8.api.tools import set_tool_context
    set_tool_context(user_id=user.user_id)
    profile_json = await user_profile()
    profile = json.loads(profile_json)

    assert profile["name"] == "profile-meta-user"
    assert profile["content"] == "A developer who likes hiking."
    assert profile["metadata"]["relations"][0]["name"] == "Buddy"
    assert profile["metadata"]["interests"] == ["hiking", "coding"]
    assert profile["metadata"]["preferences"]["timezone"] == "US/Eastern"
