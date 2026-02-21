"""Token estimation via tiktoken.

Replaces the ``len(text) // 4`` heuristic used across the codebase with
accurate BPE token counts. The encoder is cached per model for performance.

Examples::

    from p8.utils.tokens import estimate_tokens

    estimate_tokens("Hello, world!")          # accurate count
    estimate_tokens(None)                     # 0
    estimate_tokens("long text", model="gpt-4o")
"""

from __future__ import annotations

import tiktoken

_encoder_cache: dict[str, tiktoken.Encoding] = {}


def estimate_tokens(text: str | None, model: str = "gpt-4o") -> int:
    """Count tokens using tiktoken. Returns 0 for empty/None text.

    Caches the encoder per model for performance. Falls back to
    ``cl100k_base`` if the model name isn't recognised by tiktoken.

    Args:
        text: The string to tokenise. ``None`` or empty → 0.
        model: OpenAI model name used to select the right BPE encoding.

    Returns:
        Token count (int).
    """
    if not text:
        return 0

    if model not in _encoder_cache:
        try:
            _encoder_cache[model] = tiktoken.encoding_for_model(model)
        except KeyError:
            _encoder_cache[model] = tiktoken.get_encoding("cl100k_base")

    return len(_encoder_cache[model].encode(text))
