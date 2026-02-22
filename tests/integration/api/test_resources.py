"""Tests for resources endpoints + feed integration."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from p8.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


def _insert_resource(client, name: str, category: str, **kwargs):
    """Helper: insert a resource via SQL."""
    uri = kwargs.get("uri", f"https://example.com/{name}")
    image_uri = kwargs.get("image_uri", "")
    comment = kwargs.get("comment", "")
    content = kwargs.get("content", f"Content for {name}")
    user_id = kwargs.get("user_id", "")

    user_clause = f"'{user_id}'::uuid" if user_id else "NULL"
    image_clause = f"'{image_uri}'" if image_uri else "NULL"
    comment_clause = f"'{comment}'" if comment else "NULL"

    resp = client.post("/query/", json={
        "mode": "SQL",
        "query": f"""
            INSERT INTO resources (name, uri, content, category, image_uri, comment, user_id, related_entities)
            VALUES ('{name}', '{uri}', '{content}', '{category}', {image_clause}, {comment_clause}, {user_clause}, '{{}}')
            RETURNING id::text
        """,
    })
    assert resp.status_code == 200
    rows = resp.json()
    return rows[0]["id"] if rows else None


def test_list_resources_empty(client):
    resp = client.get("/resources/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_resources_crud(client):
    # Insert a resource
    rid = _insert_resource(client, "test-news-1", "news",
                           image_uri="https://img.example.com/1.jpg",
                           comment="Breaking story")
    assert rid is not None

    # GET /resources/{id}
    resp = client.get(f"/resources/{rid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test-news-1"
    assert data["category"] == "news"
    assert data["image_uri"] == "https://img.example.com/1.jpg"
    assert data["comment"] == "Breaking story"

    # GET /resources/?category=news
    resp = client.get("/resources/", params={"category": "news"})
    assert resp.status_code == 200
    items = resp.json()
    assert any(r["name"] == "test-news-1" for r in items)

    # GET /resources/?category=research — should not include news
    resp = client.get("/resources/", params={"category": "research"})
    assert resp.status_code == 200
    items = resp.json()
    assert not any(r["name"] == "test-news-1" for r in items)


def test_resource_categories(client):
    _insert_resource(client, "cat-news-1", "news")
    _insert_resource(client, "cat-news-2", "news")
    _insert_resource(client, "cat-research-1", "research")

    resp = client.get("/resources/categories")
    assert resp.status_code == 200
    cats = resp.json()
    cat_map = {c["category"]: c["count"] for c in cats}
    assert cat_map.get("news", 0) >= 2
    assert cat_map.get("research", 0) >= 1


def test_resource_not_found(client):
    resp = client.get("/resources/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_feed_includes_resource_counts(client):
    """Insert a resource with category and verify the feed's daily summary includes resource_counts."""
    _insert_resource(client, "feed-news-1", "news")

    resp = client.get("/moments/feed", params={"limit": 5})
    assert resp.status_code == 200
    feed = resp.json()

    # Find a daily_summary for today
    summaries = [f for f in feed if f["event_type"] == "daily_summary"]
    assert len(summaries) > 0

    # At least one summary should have resource_counts with news
    has_resource_counts = any(
        s.get("metadata", {}).get("resource_counts", {}).get("news", 0) > 0
        for s in summaries
    )
    assert has_resource_counts, f"No summary with news resource_counts found in {summaries}"
