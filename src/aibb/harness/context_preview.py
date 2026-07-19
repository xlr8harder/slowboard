"""Operator-readable and exact previews of a run's next model context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aibb.harness.compaction import estimate_message_tokens
from aibb.sessions import SessionStore


def canonical_run_context(run_dir: Path) -> dict[str, Any]:
    """Return the persisted Harn context plus the run's immutable tool definitions."""

    run_dir = run_dir.resolve()
    run_id = run_dir.name
    store = SessionStore(run_dir / "session", run_id)
    checkpoint = store.read_checkpoint()
    envelope = next((event.payload for event in store.read_events() if event.type == "context_envelope"), None)
    if envelope is None:
        raise ValueError(f"Run has no recorded context envelope: {run_id}")
    compactions = [event.payload for event in store.read_events() if event.type == "compaction_applied"]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "checkpoint_event_sequence": checkpoint.event_sequence,
        "context_generation": checkpoint.engine.context_generation,
        "estimated_message_tokens": estimate_message_tokens(checkpoint.engine.messages),
        "system_prompt": checkpoint.engine.system_prompt,
        "messages": checkpoint.engine.messages,
        "tools": envelope.get("tool_definitions", []),
        "compactions": compactions,
    }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _render_content(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return [_json(content)]
    rendered: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            rendered.append(_json(block))
            continue
        block_type = str(block.get("type") or "content")
        if block_type in {"text", "output_text"}:
            rendered.append(str(block.get("text") or ""))
        elif block_type == "thinking":
            rendered.append("[thinking]\n" + str(block.get("thinking") or ""))
        elif block_type == "toolCall":
            rendered.append(
                f"[tool call {block.get('name', 'unknown')} id={block.get('id', 'unknown')}]\n"
                + _json(block.get("arguments") or {})
            )
        elif block_type in {"image", "image_url"}:
            media_type = block.get("mediaType") or block.get("media_type") or "unknown"
            rendered.append(f"[image content presented to the model; media_type={media_type}]")
        else:
            rendered.append(f"[{block_type}]\n{_json(block)}")
    return rendered


def render_run_context(context: dict[str, Any]) -> str:
    """Render every text/tool message without truncation for private operator review."""

    lines = [
        "SLOWBOARD MODEL CONTEXT PREVIEW",
        f"run_id: {context['run_id']}",
        f"context_generation: {context['context_generation']}",
        f"checkpoint_event_sequence: {context['checkpoint_event_sequence']}",
        f"estimated_message_tokens: {context['estimated_message_tokens']}",
        f"message_count: {len(context['messages'])}",
        f"tool_count: {len(context['tools'])}",
        f"compaction_count: {len(context['compactions'])}",
        "",
        "===== SYSTEM PROMPT =====",
        context["system_prompt"] or "[empty: this run has no custom system prompt]",
    ]
    for index, message in enumerate(context["messages"]):
        role = message.get("role", "unknown")
        details = []
        for key in ("toolName", "toolCallId", "isError"):
            if key in message:
                details.append(f"{key}={message[key]}")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.extend(["", f"===== MESSAGE {index} · {role}{suffix} ====="])
        lines.extend(_render_content(message.get("content")))
    lines.extend(["", "===== AVAILABLE TOOLS ====="])
    for index, tool in enumerate(context["tools"]):
        lines.extend(
            [
                "",
                f"--- TOOL {index} · {tool.get('name', 'unknown')} ---",
                str(tool.get("description") or ""),
                _json(tool.get("parameters") or {}),
            ]
        )
    if context["compactions"]:
        lines.extend(["", "===== COMPACTION EVENTS =====", _json(context["compactions"])])
    return "\n".join(lines).rstrip() + "\n"
