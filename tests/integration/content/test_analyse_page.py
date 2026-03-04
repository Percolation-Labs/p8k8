"""Tests for POST /content/analyse and PATCH /moments metadata merge.

1. Upload a file → get a moment
2. PATCH moment with annotation metadata (shallow merge)
3. POST /content/analyse with image or content descriptor → LLM analysis

The vision call is mocked to avoid real LLM costs in CI.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

USER_ID = "00000000-0000-0000-0000-000000000001"
HEADERS = {
    "x-user-id": USER_ID,
    "x-user-email": "test@example.com",
    "x-tenant-id": "system",
}

MOCK_ANALYSIS_RESULT = {
    "explanation": "This page shows a diagram of system architecture.",
    "model": "mock-model",
    "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    "item_count": 1,
}


@pytest.fixture(scope="module")
def client():
    from p8.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def test_image_bytes() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (200, 200), color=(80, 120, 200))
    draw = ImageDraw.Draw(img)
    draw.text((20, 80), "Test page content", fill="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _upload_and_get_moment(client, filename: str, content: bytes) -> tuple[str, str]:
    """Upload a file and return (file_id, moment_id)."""
    resp = client.post(
        "/content/",
        files={"file": (filename, content, "text/plain")},
        headers=HEADERS,
    )
    assert resp.status_code == 201, f"Upload failed: {resp.text}"
    file_id = resp.json()["file"]["id"]

    # Find the moment in the feed — feed items use event_id
    feed = client.get("/moments/feed", headers=HEADERS).json()
    results = feed if isinstance(feed, list) else feed.get("results", [])
    for m in results:
        if not isinstance(m, dict):
            continue
        meta = m.get("metadata", {})
        if isinstance(meta, dict):
            fid = meta.get("file_id") or (meta.get("moment_metadata") or {}).get("file_id")
            if fid == file_id:
                moment_id = m.get("event_id") or m.get("id")
                return file_id, str(moment_id)

    pytest.fail(f"No moment found for file {file_id}")


# --------------------------------------------------------------------------
# 1. PATCH /moments — metadata merge + topic_tags
# --------------------------------------------------------------------------


def test_patch_metadata_merge(client):
    """Upload a text file, then PATCH annotation metadata with shallow merge."""
    file_id, moment_id = _upload_and_get_moment(client, "patch-test.txt", b"Patch test content")

    annotations = {
        "stickers": [
            {"type": "text", "content": "Important!", "x": 100, "y": 200},
        ],
        "drawing_stroke_count": 12,
    }
    resp = client.patch(
        f"/moments/{moment_id}",
        json={"metadata": annotations},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"PATCH failed: {resp.text}"
    updated = resp.json()
    meta = updated.get("metadata", {})
    assert meta.get("stickers") == annotations["stickers"]
    assert meta.get("drawing_stroke_count") == 12


def test_patch_metadata_merge_preserves_existing(client):
    """Second PATCH should merge, not replace."""
    _, moment_id = _upload_and_get_moment(client, "merge-test.txt", b"Merge test")

    # First patch
    client.patch(
        f"/moments/{moment_id}",
        json={"metadata": {"key_a": "aaa"}},
        headers=HEADERS,
    )
    # Second patch — should preserve key_a
    resp = client.patch(
        f"/moments/{moment_id}",
        json={"metadata": {"key_b": "bbb"}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    meta = resp.json().get("metadata", {})
    assert meta.get("key_a") == "aaa", "First key lost after merge"
    assert meta.get("key_b") == "bbb"


def test_patch_topic_tags(client):
    """PATCH topic_tags onto a moment."""
    _, moment_id = _upload_and_get_moment(client, "tags-test.txt", b"Tags test")

    resp = client.patch(
        f"/moments/{moment_id}",
        json={"topic_tags": ["drawing", "sketch", "homework"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json().get("topic_tags") == ["drawing", "sketch", "homework"]


# --------------------------------------------------------------------------
# 2. POST /content/analyse — image mode
# --------------------------------------------------------------------------


def test_analyse_image_upload(client, test_image_bytes):
    """Send an uploaded image for analysis."""
    with patch("p8.services.vision.analyse_content", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_ANALYSIS_RESULT.copy()

        resp = client.post(
            "/content/analyse",
            files={"image": ("page.png", test_image_bytes, "image/png")},
            data={"query": "What is on this page?"},
            headers=HEADERS,
        )

    assert resp.status_code == 200, f"Analyse failed: {resp.text}"
    result = resp.json()
    assert result["explanation"] == MOCK_ANALYSIS_RESULT["explanation"]
    assert result["usage"]["total_tokens"] == 150

    mock.assert_called_once()
    call_items = mock.call_args[0][0]
    assert len(call_items) == 1
    assert call_items[0].image_data == test_image_bytes


def test_analyse_empty_image(client):
    """Empty image upload should return 400."""
    resp = client.post(
        "/content/analyse",
        files={"image": ("empty.png", b"", "image/png")},
        data={"query": "Explain"},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_analyse_no_content(client):
    """No image and no descriptor should return 400."""
    resp = client.post(
        "/content/analyse",
        data={"query": "Explain"},
        headers=HEADERS,
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# 3. POST /content/analyse — descriptor mode
# --------------------------------------------------------------------------


def test_analyse_descriptor_with_page_selection(client):
    """Descriptor with resource ID and page selection."""
    descriptor = {
        "flow": "pdf_page",
        "resources": [
            {"id": "00000000-0000-0000-0000-000000000099", "pages": [12, 13]},
        ],
        "context": {"subject": "math homework", "grade": "5th"},
    }

    with patch("p8.services.vision.analyse_content", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_ANALYSIS_RESULT.copy()

        resp = client.post(
            "/content/analyse",
            data={
                "query": "Explain pages 12-13",
                "descriptor": json.dumps(descriptor),
            },
            headers=HEADERS,
        )

    assert resp.status_code == 200
    result = resp.json()
    assert result["flow"] == "pdf_page"

    call_items = mock.call_args[0][0]
    assert any(item.uri == "00000000-0000-0000-0000-000000000099" for item in call_items)
    assert any(getattr(item, "pages", None) == [12, 13] for item in call_items)
    assert any(item.text and "math homework" in item.text for item in call_items)


def test_analyse_descriptor_simple_resource_ids(client):
    """Descriptor with plain string resource IDs."""
    descriptor = {
        "flow": "generic",
        "resources": ["00000000-0000-0000-0000-000000000099"],
    }

    with patch("p8.services.vision.analyse_content", new_callable=AsyncMock) as mock:
        mock.return_value = MOCK_ANALYSIS_RESULT.copy()

        resp = client.post(
            "/content/analyse",
            data={
                "query": "Summarise this document",
                "descriptor": json.dumps(descriptor),
            },
            headers=HEADERS,
        )

    assert resp.status_code == 200
    call_items = mock.call_args[0][0]
    assert any(item.uri == "00000000-0000-0000-0000-000000000099" for item in call_items)


def test_analyse_invalid_descriptor(client):
    """Invalid JSON descriptor should return 400."""
    resp = client.post(
        "/content/analyse",
        data={"descriptor": "not json"},
        headers=HEADERS,
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# 4. Real LLM — PDF page analysis (run with: pytest -m llm)
# --------------------------------------------------------------------------


@pytest.mark.llm
def test_analyse_real_pdf_with_llm(client):
    """Upload a real PDF, render page 0, send to vision LLM."""
    from pathlib import Path

    import fitz

    pdf_path = Path(__file__).resolve().parent.parent.parent / "data" / "uploads" / "sample-report.pdf"
    if not pdf_path.exists():
        pytest.skip(f"Test PDF not found: {pdf_path}")

    pdf_bytes = pdf_path.read_bytes()

    # Render page 0
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=150)
    page_png = pix.tobytes("png")
    doc.close()
    assert len(page_png) > 100, "Page render produced no data"

    # Upload PDF
    resp = client.post(
        "/content/",
        files={"file": ("sample-report.pdf", pdf_bytes, "application/pdf")},
        headers=HEADERS,
    )
    assert resp.status_code == 201

    # Analyse rendered page — real LLM call
    resp = client.post(
        "/content/analyse",
        files={"image": ("page0.png", page_png, "image/png")},
        data={"query": "What is on this page? Answer in one sentence."},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"Analyse failed: {resp.text}"
    result = resp.json()

    assert "explanation" in result
    assert len(result["explanation"]) > 10, f"Explanation too short: {result['explanation']}"
    assert result["usage"]["total_tokens"] > 0
    print(f"\nLLM response: {result['explanation']}")
    print(f"Model: {result['model']}, tokens: {result['usage']['total_tokens']}")
