"""Transport-aware context estimates shared by provider and compaction paths."""

from __future__ import annotations

import json
from typing import Any

ESTIMATED_IMAGE_INPUT_TOKENS = 4_096


def estimate_json_tokens(value: Any) -> int:
    """Estimate JSON-carried context without tokenizing encoded image bytes."""

    image_count = 0

    def scrub(item: Any) -> Any:
        nonlocal image_count
        if isinstance(item, dict):
            if item.get("type") == "image" and isinstance(item.get("data"), str):
                image_count += 1
                return {**item, "data": "[encoded image input]"}
            return {key: scrub(nested) for key, nested in item.items()}
        if isinstance(item, list):
            return [scrub(nested) for nested in item]
        if isinstance(item, str) and item.startswith("data:image/") and ";base64," in item:
            image_count += 1
            return "[encoded image input]"
        return item

    encoded = json.dumps(
        scrub(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    text_tokens = max(1, (len(encoded) + 3) // 4)
    return text_tokens + image_count * ESTIMATED_IMAGE_INPUT_TOKENS
