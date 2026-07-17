"""Deterministic constrained Markdown shared by validation, preview, and publication."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token


class MarkdownValidationError(ValueError):
    """Raised when contribution Markdown uses syntax outside the public profile."""


_BLOCK_TOKENS = {
    "paragraph_open",
    "paragraph_close",
    "inline",
    "blockquote_open",
    "blockquote_close",
    "bullet_list_open",
    "bullet_list_close",
    "ordered_list_open",
    "ordered_list_close",
    "list_item_open",
    "list_item_close",
    "fence",
}
_INLINE_TOKENS = {
    "text",
    "softbreak",
    "em_open",
    "em_close",
    "strong_open",
    "strong_close",
    "link_open",
    "link_close",
}
_ALLOWED_LINK_SCHEMES = {"", "http", "https"}
_VALIDATOR = MarkdownIt("commonmark", {"html": True})
_RENDERER = MarkdownIt("commonmark", {"html": False})


def _link_href(token: Token) -> str:
    value = token.attrGet("href")
    return value or ""


def _validate_tokens(tokens: list[Token]) -> None:
    for token in tokens:
        if token.type in {"html_block", "html_inline"}:
            raise MarkdownValidationError("raw HTML is not allowed")
        if token.type not in _BLOCK_TOKENS:
            raise MarkdownValidationError(f"unsupported Markdown syntax: {token.type}")
        for child in token.children or []:
            if child.type in {"html_block", "html_inline"}:
                raise MarkdownValidationError("raw HTML is not allowed")
            if child.type not in _INLINE_TOKENS:
                raise MarkdownValidationError(f"unsupported Markdown syntax: {child.type}")
            if child.type == "link_open":
                href = _link_href(child)
                if urlsplit(href).scheme.casefold() not in _ALLOWED_LINK_SCHEMES:
                    raise MarkdownValidationError("links must use HTTP(S), an archive-relative path, or a fragment")


def validate_contribution_markdown(value: str) -> None:
    _validate_tokens(_VALIDATOR.parse(value))


def render_contribution_markdown(value: str) -> str:
    validate_contribution_markdown(value)
    return _RENDERER.render(value)


def contribution_excerpt(value: str, limit: int = 220) -> str:
    tokens = _VALIDATOR.parse(value)
    _validate_tokens(tokens)
    pieces: list[str] = []
    for token in tokens:
        if token.type == "inline":
            pieces.extend(child.content for child in token.children or [] if child.type in {"text", "softbreak"})
        elif token.type == "fence":
            pieces.append(token.content)
    plain = re.sub(r"\s+", " ", " ".join(pieces)).strip()
    if len(plain) <= limit:
        return plain
    return plain[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
