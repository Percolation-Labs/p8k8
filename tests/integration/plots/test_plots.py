"""Tests for save_plot / remove_plot tools — daily plot collections."""

from __future__ import annotations

from uuid import UUID

import pytest

from p8.api.tools import init_tools, set_tool_context

USER_BOB = UUID("00000000-0000-0000-0000-000000b0b000")


@pytest.fixture(autouse=True)
async def _setup_tools(db, encryption, clean_db):
    """Initialize tool module state with live DB + encryption."""
    init_tools(db, encryption)
    set_tool_context(user_id=USER_BOB)


@pytest.fixture(autouse=True)
async def _cleanup_plots(db):
    """Remove plot_collection moments after each test."""
    yield
    await db.execute(
        "DELETE FROM moments WHERE moment_type = 'plot_collection' AND user_id = $1",
        USER_BOB,
    )


# ---------------------------------------------------------------------------
# save_plot — first plot creates collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_plot_creates_collection(db):
    from p8.api.tools.plots import save_plot

    result = await save_plot(
        title="API Flow",
        source="graph LR; A-->B; B-->C;",
        plot_type="mermaid",
        topic_tags=["architecture"],
    )

    assert result["status"] == "success"
    assert result["plot_count"] == 1
    assert len(result["plot_id"]) == 8
    assert result["collection_name"].startswith("plots-")

    # Verify moment exists in DB
    row = await db.fetchrow(
        "SELECT * FROM moments WHERE id = $1::uuid",
        UUID(result["moment_id"]),
    )
    assert row is not None
    assert row["moment_type"] == "plot_collection"


# ---------------------------------------------------------------------------
# save_plot — second plot appends to same collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_plot_appends_to_existing(db):
    from p8.api.tools.plots import save_plot

    r1 = await save_plot(title="Chart A", source="graph TD; X-->Y;")
    r2 = await save_plot(title="Chart B", source="graph TD; Y-->Z;", topic_tags=["data"])

    assert r1["status"] == "success"
    assert r2["status"] == "success"
    assert r2["plot_count"] == 2
    # Same collection moment
    assert r1["moment_id"] == r2["moment_id"]
    # Different plot IDs
    assert r1["plot_id"] != r2["plot_id"]

    # Verify tags merged
    row = await db.fetchrow(
        "SELECT topic_tags FROM moments WHERE id = $1::uuid",
        UUID(r1["moment_id"]),
    )
    assert "data" in row["topic_tags"]


# ---------------------------------------------------------------------------
# save_plot — invalid plot_type rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_plot_invalid_type():
    from p8.api.tools.plots import save_plot

    result = await save_plot(
        title="Bad Plot",
        source="...",
        plot_type="excel",
    )

    assert result["status"] == "error"
    assert "Invalid plot_type" in result["error"]


# ---------------------------------------------------------------------------
# save_plot — no user context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_plot_no_user():
    from p8.api.tools.plots import save_plot

    set_tool_context(user_id=None)

    result = await save_plot(title="No User", source="graph LR; A-->B;")
    assert result["status"] == "error"
    assert "user_id" in result["error"]


# ---------------------------------------------------------------------------
# remove_plot — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_plot_success(db):
    from p8.api.tools.plots import remove_plot, save_plot

    r1 = await save_plot(title="Keep Me", source="graph LR; A-->B;")
    r2 = await save_plot(title="Remove Me", source="graph LR; X-->Y;")
    assert r2["plot_count"] == 2

    result = await remove_plot(plot_id=r2["plot_id"])
    assert result["status"] == "success"
    assert result["removed_title"] == "Remove Me"
    assert result["remaining_count"] == 1

    # Verify DB state
    import json
    row = await db.fetchrow(
        "SELECT metadata FROM moments WHERE id = $1::uuid",
        UUID(r1["moment_id"]),
    )
    meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
    assert len(meta["plots"]) == 1
    assert meta["plots"][0]["plot_id"] == r1["plot_id"]


# ---------------------------------------------------------------------------
# remove_plot — not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_plot_not_found():
    from p8.api.tools.plots import remove_plot, save_plot

    await save_plot(title="Exists", source="graph LR; A-->B;")

    result = await remove_plot(plot_id="deadbeef")
    assert result["status"] == "error"
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# remove_plot — no collection for date
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_plot_no_collection():
    from p8.api.tools.plots import remove_plot

    result = await remove_plot(plot_id="abcd1234", collection_date="2020-01-01")
    assert result["status"] == "error"
    assert "No plot collection" in result["error"]


# ---------------------------------------------------------------------------
# Collection appears in moment queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collection_queryable_by_type(db):
    from p8.api.tools.plots import save_plot

    await save_plot(title="Queryable", source="graph LR; A-->B;")

    rows = await db.fetch(
        "SELECT * FROM moments WHERE moment_type = 'plot_collection' AND user_id = $1",
        USER_BOB,
    )
    assert len(rows) == 1
    assert rows[0]["name"].startswith("plots-")
