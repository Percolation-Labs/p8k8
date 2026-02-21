"""Integration test — image upload → thumbnail generation → feed with image."""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from p8.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def test_image_bytes() -> bytes:
    """Generate a small 200x150 red PNG for testing."""
    from PIL import Image

    img = Image.new("RGB", (200, 150), color="red")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


USER_ID = "00000000-0000-0000-0000-000000000001"
HEADERS = {
    "x-user-id": USER_ID,
    "x-user-email": "test@example.com",
    "x-tenant-id": "system",
}


def test_image_upload_creates_thumbnail_and_feed(client, test_image_bytes):
    """Upload an image, verify base64 thumbnail on moment and in feed."""

    # 1. Upload the image
    resp = client.post(
        "/content/",
        files={"file": ("test-photo.png", test_image_bytes, "image/png")},
        headers=HEADERS,
    )
    assert resp.status_code == 201, f"Upload failed: {resp.text}"
    upload_data = resp.json()
    file_data = upload_data["file"]
    file_id = file_data["id"]
    print(f"\n  Upload OK — file_id={file_id}")

    # 2. Check thumbnail_uri was set on the file entity (S3 backup)
    has_thumbnail = file_data.get("thumbnail_uri") is not None
    print(f"  thumbnail_uri on file: {file_data.get('thumbnail_uri')}")

    # 3. Check the upload moment has a base64 data URI as image
    resp = client.get(
        "/moments/",
        params={"moment_type": "content_upload", "limit": 5},
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"Moments query failed: {resp.text}"
    moments = resp.json()
    assert len(moments) >= 1, "No content_upload moments found"

    upload_moment = None
    for m in moments:
        meta = m.get("metadata") or {}
        if meta.get("file_id") == file_id:
            upload_moment = m
            break

    assert upload_moment is not None, f"No moment found for file_id={file_id}"
    image = upload_moment.get("image")
    assert image is not None, "image not set on content_upload moment"
    assert image.startswith("data:image/jpeg;base64,"), f"Expected data URI, got: {image[:60]}"

    # Verify the base64 decodes to valid JPEG
    b64_data = image.split(",", 1)[1]
    thumb_bytes = base64.b64decode(b64_data)
    assert thumb_bytes[:2] == b"\xff\xd8", "Decoded data is not a valid JPEG"
    print(f"  Moment image: data:image/jpeg;base64,... ({len(b64_data)} chars, {len(thumb_bytes)} bytes)")

    # 4. Check metadata has the API path fallback for full-res
    meta = upload_moment.get("metadata", {})
    assert meta.get("image_url") == f"/content/files/{file_id}?thumbnail=true"
    print(f"  Metadata image_url (API fallback): {meta['image_url']}")

    # 5. Thumbnail endpoint still works for full-res access
    resp = client.get(
        f"/content/files/{file_id}?thumbnail=true",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    if has_thumbnail:
        assert resp.headers["content-type"] == "image/jpeg"
        assert "max-age=86400" in resp.headers.get("cache-control", "")
        print(f"  Thumbnail endpoint: {len(resp.content)} bytes, type={resp.headers['content-type']}")
    else:
        print(f"  No S3 thumbnail — fell back to original: {len(resp.content)} bytes")

    # 6. Original file still served
    resp = client.get(f"/content/files/{file_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    print(f"  Original served: {len(resp.content)} bytes")

    # 7. Feed includes image as base64 data URI
    resp = client.get("/moments/feed", params={"limit": 20}, headers=HEADERS)
    assert resp.status_code == 200
    feed = resp.json()

    feed_moment = None
    for entry in feed:
        if entry.get("event_type") == "moment":
            meta = entry.get("metadata") or {}
            moment_meta = meta.get("moment_metadata") or {}
            if moment_meta.get("file_id") == file_id:
                feed_moment = entry
                break

    assert feed_moment is not None, f"Upload moment not found in feed for file_id={file_id}"
    feed_image = feed_moment.get("image")
    assert feed_image is not None, "image missing from feed"
    assert feed_image.startswith("data:image/jpeg;base64,"), "Feed image should be a data URI"
    print(f"  Feed image: data:image/jpeg;base64,... ({len(feed_image)} chars)")
    print("  All checks passed!")
