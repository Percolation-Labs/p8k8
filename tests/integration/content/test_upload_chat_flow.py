"""End-to-end test: upload a document → check feed → chat about it.

Verifies the agent can answer "what is this?" using session context from
an uploaded file. Requires a real LLM (marked with @pytest.mark.llm).
"""

from __future__ import annotations

from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

USER_ID = "00000000-0000-0000-0000-000000000001"
HEADERS = {
    "x-user-id": USER_ID,
    "x-user-email": "test@example.com",
    "x-tenant-id": "system",
}


@pytest.fixture(scope="module")
def client():
    from p8.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def test_image_bytes() -> bytes:
    """Generate a small test image with embedded text for context."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 300), color=(30, 60, 120))
    draw = ImageDraw.Draw(img)
    draw.text((50, 130), "Percolate Architecture Diagram", fill="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _parse_sse_text(raw: str) -> str:
    """Extract the assistant's text from AG-UI SSE events."""
    import json

    text_parts = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if event.get("type") == "TEXT_MESSAGE_CONTENT":
            text_parts.append(event.get("delta", ""))
    return "".join(text_parts)


# --------------------------------------------------------------------------
# 1. Upload + feed (no LLM needed)
# --------------------------------------------------------------------------


def test_upload_and_feed(client, test_image_bytes):
    """Upload an image and verify the feed contains it with image."""

    # Upload
    resp = client.post(
        "/content/",
        files={"file": ("architecture-diagram.png", test_image_bytes, "image/png")},
        headers=HEADERS,
    )
    assert resp.status_code == 201, f"Upload failed: {resp.text}"
    upload_data = resp.json()
    file_id = upload_data["file"]["id"]
    session_id = upload_data.get("session_id")
    assert session_id, "Upload should return a session_id"
    print(f"\n  Uploaded file_id={file_id}, session_id={session_id}")

    # Check feed
    resp = client.get("/moments/feed", params={"limit": 20}, headers=HEADERS)
    assert resp.status_code == 200
    feed = resp.json()

    upload_entry = None
    for entry in feed:
        if entry.get("event_type") == "moment":
            meta = entry.get("metadata") or {}
            mm = meta.get("moment_metadata") or {}
            if mm.get("file_id") == file_id:
                upload_entry = entry
                break

    assert upload_entry is not None, "Upload moment not in feed"
    feed_image = upload_entry.get("image")
    assert feed_image is not None, "image missing from feed"
    assert feed_image.startswith("data:image/jpeg;base64,"), "Expected base64 data URI"
    print(f"  Feed image: data:image/jpeg;base64,... ({len(feed_image)} chars)")

    # Store for next test
    test_upload_and_feed._file_id = file_id
    test_upload_and_feed._session_id = session_id


# --------------------------------------------------------------------------
# 2. Chat on the upload session (requires LLM)
# --------------------------------------------------------------------------


@pytest.mark.llm
def test_chat_about_upload(client):
    """Chat on the upload session — agent should know what was uploaded."""

    session_id = getattr(test_upload_and_feed, "_session_id", None)
    if not session_id:
        pytest.skip("test_upload_and_feed must run first")

    body = {
        "thread_id": session_id,
        "run_id": str(uuid4()),
        "state": {},
        "messages": [
            {
                "id": str(uuid4()),
                "role": "user",
                "content": "what is this?",
            },
        ],
        "tools": [],
        "context": [],
        "forwarded_props": {},
    }

    resp = client.post(
        f"/chat/{session_id}",
        json=body,
        headers={
            **HEADERS,
            "accept": "text/event-stream",
        },
    )
    assert resp.status_code == 200, f"Chat failed: {resp.text}"

    assistant_text = _parse_sse_text(resp.text)
    print(f"\n  User: what is this?")
    print(f"  Agent: {assistant_text[:300]}")

    # The agent should reference the upload in some way
    text_lower = assistant_text.lower()
    assert len(assistant_text) > 20, f"Agent response too short: {assistant_text!r}"
    # The agent should mention something about the file/upload/image/document
    context_words = ["upload", "file", "image", "document", "architecture", "diagram", "photo", "png", "picture"]
    found = [w for w in context_words if w in text_lower]
    assert found, (
        f"Agent didn't reference the upload context. "
        f"Response: {assistant_text[:200]}"
    )
    print(f"  Context words found: {found}")
    print("  Agent understands the upload context!")
