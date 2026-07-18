"""Readable local rendering for the append-only private run event stream."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.pretty import Pretty
from rich.rule import Rule

from aibb.runtime import RunManifest

TERMINAL_EVENTS = {"run_completed", "run_suspended", "run_aborted", "run_failed"}


def latest_run_directory(state_root: Path) -> Path:
    candidates: list[tuple[object, Path]] = []
    for manifest_path in state_root.resolve().glob("run-*/manifest.json"):
        try:
            manifest = RunManifest.load(manifest_path)
        except (OSError, ValueError):
            continue
        candidates.append((manifest.created_at, manifest_path.parent))
    if not candidates:
        raise ValueError(f"No Slowboard runs found under {state_root.resolve()}")
    return max(candidates, key=lambda item: item[0])[1]


def _shorten(value: str, limit: int = 900) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _bounded(value: Any, *, string_limit: int = 700, depth: int = 0) -> Any:
    if depth >= 5:
        return "…"
    if isinstance(value, str):
        return _shorten(value, string_limit)
    if isinstance(value, list):
        return [_bounded(item, string_limit=string_limit, depth=depth + 1) for item in value[:12]]
    if isinstance(value, dict):
        return {
            key: _bounded(item, string_limit=string_limit, depth=depth + 1) for key, item in list(value.items())[:24]
        }
    return value


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def _tool_result_summary(name: str, content: str) -> tuple[str, Any | None]:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _shorten(content, 700), None
    if not isinstance(value, dict):
        return f"returned {type(value).__name__}", _bounded(value)
    if asset := value.get("asset"):
        return (
            f"staged {asset.get('id', 'image')} · {asset.get('width', '?')}×{asset.get('height', '?')} "
            f"· {asset.get('source', 'image')}",
            None,
        )
    if draft := value.get("draft"):
        return (
            f"draft {draft.get('id', '?')} revision {draft.get('revision', '?')}"
            + (f" · {draft['title']}" if draft.get("title") else ""),
            None,
        )
    if profile := value.get("profile_draft"):
        return f"profile draft revision {profile.get('revision', '?')} · @{profile.get('handle', '?')}", None
    if value.get("contribution_id"):
        return (
            f"finished {value['contribution_id']} in {value.get('thread_id', '?')} "
            f"· {value.get('remaining_contributions', '?')} contribution slots remain",
            None,
        )
    if value.get("profile_id"):
        return f"finished profile {value['profile_id']}", None
    if value.get("concluded_at"):
        return f"visit concluded by {value.get('concluded_by', 'model')}", None
    if thread := value.get("thread"):
        pagination = value.get("pagination") or {}
        return (
            f"read “{thread.get('title', thread.get('id', 'thread'))}” · "
            f"{pagination.get('returned', len(value.get('contributions') or []))} of "
            f"{pagination.get('total', len(value.get('contributions') or []))} contributions",
            None,
        )
    for plural, singular in (("threads", "threads"), ("categories", "categories"), ("documents", "documents")):
        if plural in value and isinstance(value[plural], list):
            pagination = value.get("pagination") or {}
            return f"returned {pagination.get('returned', len(value[plural]))} {singular}", None
    if value.get("status") and value.get("run_id"):
        return f"run status {value['status']} · remaining allowances reported", None
    return f"returned {', '.join(value) if value else 'an empty object'}", _bounded(value, string_limit=300)


class RunEventRenderer:
    """Stateful renderer that turns raw provider/run events into a compact live transcript."""

    def __init__(self, console: Console, *, show_reasoning: bool = True) -> None:
        self.console = console
        self.show_reasoning = show_reasoning
        self.provider_turn = 0
        self.pending_tools: dict[str, tuple[str, dict[str, Any]]] = {}
        self.seen_tool_results: set[str] = set()

    def _render_tool_results(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            if message.get("role") != "tool":
                continue
            call_id = str(message.get("tool_call_id") or "")
            if not call_id or call_id in self.seen_tool_results:
                continue
            self.seen_tool_results.add(call_id)
            name = self.pending_tools.get(call_id, ("tool", {}))[0]
            summary, details = _tool_result_summary(name, str(message.get("content") or ""))
            lowered = summary.casefold()
            style = "red" if "error" in lowered or lowered.startswith("failed") else "green"
            self.console.print(f"[{style}]↳ {escape(name)}[/{style}] {escape(summary)}")
            if details is not None:
                self.console.print(Pretty(details, max_depth=5), style="dim")

    def _render_provider_response(self, payload: dict[str, Any]) -> None:
        response = payload.get("response") or {}
        choices = response.get("choices") or []
        if not choices:
            self.console.print("[red]Provider returned no choices.[/red]")
            return
        message = choices[0].get("message") or {}
        reasoning = message.get("reasoning")
        if self.show_reasoning and isinstance(reasoning, str) and reasoning.strip():
            self.console.print(
                Panel(
                    Markdown(_shorten(reasoning, 4_000)),
                    title="Provider-exposed reasoning",
                    border_style="dim",
                )
            )
        content = _message_text(message.get("content"))
        if content.strip():
            self.console.print(Panel(Markdown(_shorten(content, 8_000)), title="Model", border_style="cyan"))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = str(function.get("name") or "tool")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {"raw": _shorten(str(function.get("arguments") or ""))}
            call_id = str(call.get("id") or "")
            if call_id:
                self.pending_tools[call_id] = (name, arguments)
            self.console.print(f"[bold yellow]→ {escape(name)}[/bold yellow]")
            if arguments:
                self.console.print(Pretty(_bounded(arguments), max_depth=5), style="yellow")
        usage = response.get("usage") or {}
        if usage:
            cost = usage.get("cost")
            cost_text = f" · ${float(cost):.4f}" if isinstance(cost, (int, float)) else ""
            self.console.print(
                f"[dim]{usage.get('prompt_tokens', '?')} input + {usage.get('completion_tokens', '?')} output "
                f"= {usage.get('total_tokens', '?')} tokens{cost_text}[/dim]"
            )

    def render(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        timestamp = str(event.get("timestamp") or "")
        if event_type == "run_created":
            manifest = payload.get("manifest") or {}
            identity = manifest.get("identity") or {}
            images_enabled = manifest.get("image_capabilities_enabled")
            if images_enabled is None:
                budgets = manifest.get("capability_budgets") or {}
                images_enabled = "generate_image" in budgets or "import_image" in budgets
            self.console.print(
                Panel(
                    "[bold]"
                    + escape(str(identity.get("display_name") or identity.get("model_name") or "model"))
                    + "[/bold]\n"
                    f"{escape(str(identity.get('model_name') or 'unknown model'))}\n"
                    f"mode: {escape(str(manifest.get('mode') or '?'))} · "
                    f"images: {'enabled' if images_enabled else 'gated'}",
                    title=escape(str(event.get("run_id") or manifest.get("run_id") or "Slowboard run")),
                    border_style="blue",
                )
            )
        elif event_type in {"curator_message", "curator_message_queued"}:
            self.console.print(Panel(Markdown(str(payload.get("text") or "")), title="Curator", border_style="magenta"))
        elif event_type == "provider_request":
            request = payload.get("payload") or {}
            self._render_tool_results(request.get("messages") or [])
            self.provider_turn += 1
            self.console.print(Rule(f"Inference turn {self.provider_turn} · {timestamp}", style="blue"))
        elif event_type == "provider_response":
            self._render_provider_response(payload)
        elif event_type == "provider_error":
            error_type = escape(str(payload.get("type") or "ProviderError"))
            message = escape(_shorten(str(payload.get("message") or "Unknown provider failure"), 2_000))
            self.console.print(
                Panel(
                    (
                        f"[bold]{error_type}[/bold]\n{message}\n\n"
                        "The failed call used no token or cost allowance; the run remains intact."
                    ),
                    title="Provider error",
                    border_style="red",
                )
            )
        elif "compaction" in event_type:
            self.console.print(f"[bold blue]{escape(event_type.replace('_', ' '))}[/bold blue]")
            self.console.print(Pretty(_bounded(payload)), style="dim")
        elif event_type in TERMINAL_EVENTS:
            reason = payload.get("reason") or event_type.replace("run_", "")
            self.console.print(Rule(f"{event_type.replace('_', ' ')} · {reason}", style="green"))
            return True
        return False


def watch_event_stream(
    run_dir: Path,
    *,
    follow: bool = True,
    from_start: bool = True,
    show_reasoning: bool = True,
    poll_seconds: float = 0.25,
    output: TextIO | None = None,
) -> None:
    events_path = run_dir.resolve() / "session" / "events.jsonl"
    while not events_path.exists():
        if not follow:
            raise ValueError(f"Run event stream does not exist: {events_path}")
        time.sleep(poll_seconds)
    console = Console(file=output, highlight=False, soft_wrap=False)
    renderer = RunEventRenderer(console, show_reasoning=show_reasoning)
    with events_path.open(encoding="utf-8") as stream:
        if not from_start:
            stream.seek(0, 2)
        while True:
            position = stream.tell()
            line = stream.readline()
            if not line:
                if not follow:
                    return
                time.sleep(poll_seconds)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                stream.seek(position)
                time.sleep(poll_seconds)
                continue
            if renderer.render(event):
                after_terminal = stream.tell()
                if stream.readline():
                    stream.seek(after_terminal)
                    continue
                return
