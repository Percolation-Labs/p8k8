"""Generic content analysis via pydantic-ai multimodal models.

Supports:
- Single image analysis (analyse_image)
- Multi-content analysis (analyse_content) — mix of images, text, and URIs
  resolved from Percolate storage (file IDs, moment IDs)
- PDF page extraction via PyMuPDF (optional dep)

Used by REST endpoints and MCP tools.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, BinaryContent

log = logging.getLogger(__name__)

def _get_model() -> str:
    """Return model for vision tasks — respects VISION_MODEL env, else uses default_model from settings."""
    if env_model := os.environ.get("VISION_MODEL"):
        return env_model
    from p8.settings import get_settings
    return get_settings().default_model


@dataclass
class ContentItem:
    """A piece of content to analyse — image bytes, plain text, or a URI to resolve."""

    # Exactly one of these should be set
    image_data: bytes | None = None
    text: str | None = None
    uri: str | None = None  # file_id, moment_id — resolved at call time

    media_type: str = "image/png"
    label: str | None = None  # optional label ("page 4", "sticker layer")
    pages: list[int] | None = None  # for PDFs: extract these pages (0-indexed)


async def analyse_image(
    image_data: bytes,
    prompt: str = "Describe what you see in this image.",
    *,
    media_type: str = "image/png",
    model: str | None = None,
) -> dict[str, Any]:
    """Analyse a single image with a vision LLM."""
    items = [ContentItem(image_data=image_data, media_type=media_type)]
    return await analyse_content(items, prompt, model=model)


async def analyse_content(
    items: list[ContentItem],
    prompt: str = "Analyse this content.",
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Analyse one or more content items with a multimodal LLM.

    Items can be images (bytes), text snippets, or URIs pointing to
    Percolate files/moments.  URIs are resolved to bytes/text before
    sending to the model.  PDF items with ``pages`` set get specific
    pages rendered to images via PyMuPDF.

    Returns:
        Dict with keys: explanation, model, usage, item_count.
    """
    model_id = model or _get_model()
    agent = Agent(model=model_id)

    # Build the message content list
    content: list = [prompt]

    for item in items:
        if item.label:
            content.append(f"\n[{item.label}]")

        if item.image_data:
            content.append(BinaryContent(data=item.image_data, media_type=item.media_type))
        elif item.text:
            content.append(item.text)
        elif item.uri:
            resolved = await _resolve_uri(item.uri, pages=item.pages)
            if isinstance(resolved, list):
                content.extend(resolved)
            elif resolved:
                content.append(resolved)
            else:
                content.append(f"(Could not resolve: {item.uri})")

    log.info("Content analysis (%s, %d items)", model_id, len(items))

    result = await agent.run(content)
    usage = _extract_usage(result)

    log.info(
        "Content analysis complete: %d chars, %s tokens",
        len(result.output), usage.get("total_tokens", "?"),
    )

    return {
        "explanation": result.output,
        "model": model_id,
        "usage": usage,
        "item_count": len(items),
    }


async def _resolve_uri(
    uri: str,
    pages: list[int] | None = None,
) -> BinaryContent | str | list[BinaryContent] | None:
    """Resolve a URI (file_id / moment_id) to content for the LLM.

    For PDFs with ``pages`` specified and PyMuPDF available, renders
    each requested page to a PNG image.  Otherwise returns the whole file.
    """
    from uuid import UUID

    from p8.api.tools import get_db, get_encryption
    from p8.ontology.types import File as FileEntity, Moment
    from p8.services.files import FileService
    from p8.services.repository import Repository
    from p8.settings import get_settings

    try:
        uid = UUID(uri)
    except ValueError:
        log.warning("Cannot resolve non-UUID URI: %s", uri)
        return None

    db = get_db()
    encryption = get_encryption()

    # Try as file first
    repo = Repository(FileEntity, db, encryption)
    entity = await repo.get(uid)

    # Maybe it's a moment — follow metadata.file_id
    if not entity:
        moment_repo = Repository(Moment, db, encryption)
        moment = await moment_repo.get(uid)
        if moment and moment.metadata and moment.metadata.get("file_id"):
            entity = await repo.get(UUID(moment.metadata["file_id"]))

    if not entity or not entity.uri:
        log.warning("Could not resolve URI %s to a file", uri)
        return None

    fs = FileService(get_settings())
    data = await fs.read(entity.uri)
    mime = (entity.mime_type or "").lower()

    # PDF with specific pages requested — render to images
    if mime == "application/pdf" and pages:
        images = _extract_pdf_pages(data, pages)
        if images:
            return images
        # Fall through to whole-file if extraction fails

    if mime.startswith("image/"):
        return BinaryContent(data=data, media_type=mime)
    elif mime == "application/pdf":
        return BinaryContent(data=data, media_type="application/pdf")
    else:
        return data.decode("utf-8", errors="replace")


def _extract_pdf_pages(pdf_bytes: bytes, pages: list[int]) -> list[BinaryContent] | None:
    """Render specific PDF pages to PNG images using PyMuPDF.

    Args:
        pdf_bytes: Raw PDF file bytes.
        pages: 0-indexed page numbers to extract.

    Returns:
        List of BinaryContent PNG images, or None if PyMuPDF unavailable.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("PyMuPDF not installed — cannot extract PDF pages, sending whole PDF")
        return None

    results = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_num in pages:
            if page_num < 0 or page_num >= len(doc):
                log.warning("Page %d out of range (PDF has %d pages)", page_num, len(doc))
                continue
            page = doc[page_num]
            pix = page.get_pixmap(dpi=150)
            png_data = pix.tobytes("png")
            results.append(BinaryContent(data=png_data, media_type="image/png"))
    finally:
        doc.close()

    return results if results else None


def _extract_usage(result) -> dict[str, int]:
    """Extract token usage from a pydantic-ai RunResult."""
    if hasattr(result, "usage") and callable(result.usage):
        u = result.usage()
        return {
            "input_tokens": getattr(u, "input_tokens", 0),
            "output_tokens": getattr(u, "output_tokens", 0),
            "total_tokens": getattr(u, "total_tokens", 0),
        }
    return {}
