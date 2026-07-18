from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from rich.console import Console
from test_budget import make_manifest

from aibb.harness import watch as watch_module
from aibb.harness.watch import RunEventRenderer, latest_run_directory, run_directories, watch_state_root


def _write_run(
    state_root: Path,
    run_id: str,
    created_at: datetime,
    display_name: str,
    *,
    completed: bool = True,
) -> Path:
    run_dir = state_root / run_id
    events_dir = run_dir / "session"
    events_dir.mkdir(parents=True)
    manifest = make_manifest().model_copy(
        update={
            "run_id": run_id,
            "created_at": created_at,
            "expires_at": created_at + timedelta(days=1),
        }
    )
    (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    events: list[dict[str, object]] = [
        {
            "type": "run_created",
            "run_id": run_id,
            "payload": {
                "manifest": {
                    "mode": "headless",
                    "identity": {"display_name": display_name, "model_name": f"example/{display_name}"},
                }
            },
        },
    ]
    if completed:
        events.append({"type": "run_completed", "run_id": run_id, "payload": {"reason": "model_concluded_visit"}})
    (events_dir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return run_dir


def _write_completed_run(state_root: Path, run_id: str, created_at: datetime, display_name: str) -> Path:
    return _write_run(state_root, run_id, created_at, display_name)


def test_run_event_renderer_shows_reasoning_tools_results_and_usage() -> None:
    output = StringIO()
    renderer = RunEventRenderer(Console(file=output, color_system=None, width=120), show_reasoning=True)
    renderer.render(
        {
            "type": "run_created",
            "run_id": "run-test",
            "payload": {
                "manifest": {
                    "mode": "headless",
                    "image_capabilities_enabled": False,
                    "identity": {"display_name": "Example Model", "model_name": "example/model"},
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_response",
            "payload": {
                "response": {
                    "choices": [
                        {
                            "message": {
                                "reasoning": "I should inspect one thread.",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "read_slowboard_thread",
                                            "arguments": '{"thread_id":"thread-one"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.01},
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_request",
            "timestamp": "2026-07-17T20:00:00Z",
            "payload": {
                "payload": {
                    "messages": [
                        {
                            "role": "tool",
                            "tool_call_id": "call-1",
                            "content": json.dumps(
                                {
                                    "thread": {"title": "A test thread"},
                                    "contributions": [{"id": "one"}],
                                    "pagination": {"returned": 1, "total": 1},
                                }
                            ),
                        }
                    ]
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_error",
            "payload": {"type": "HTTPStatusError", "message": "503 limited availability"},
        }
    )
    renderer.render(
        {
            "type": "headless_continuation_message",
            "payload": {"version": "v0.1", "text": "Continue through tools or conclude."},
        }
    )
    renderer.render({"type": "run_completed", "payload": {"reason": "model_concluded_visit"}})

    rendered = output.getvalue()
    assert "Provider-exposed reasoning" in rendered
    assert "Example Model" in rendered
    assert "images: gated" in rendered
    assert "read_slowboard_thread" in rendered
    assert "read “A test thread” · 1 of 1 contributions" in rendered
    assert "120 tokens · $0.0100" in rendered
    assert "503 limited availability" in rendered
    assert "failed call used no token or cost allowance" in rendered
    assert "Slowboard harness continuation v0.1" in rendered
    assert "Continue through tools or conclude." in rendered
    assert "run completed · model_concluded_visit" in rendered


def test_run_directories_are_manifest_ordered_and_ignore_invalid_entries(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    older = _write_completed_run(tmp_path, "run-watch-older", now, "Older")
    newer = _write_completed_run(tmp_path, "run-watch-newer", now + timedelta(seconds=1), "Newer")
    invalid = tmp_path / "run-watch-invalid"
    invalid.mkdir()
    (invalid / "manifest.json").write_text("not json\n", encoding="utf-8")

    assert run_directories(tmp_path) == [older, newer]
    assert latest_run_directory(tmp_path) == newer


def test_standing_watcher_replays_newest_then_attaches_to_new_run(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(UTC)
    _write_completed_run(tmp_path, "run-watch-old-history", now, "Old History")
    _write_completed_run(tmp_path, "run-watch-current", now + timedelta(seconds=1), "Current")
    created_next = False

    def create_next_on_wait(_: float) -> None:
        nonlocal created_next
        if not created_next:
            created_next = True
            _write_completed_run(tmp_path, "run-watch-next", now + timedelta(seconds=2), "Next")

    monkeypatch.setattr(watch_module.time, "sleep", create_next_on_wait)
    output = StringIO()

    watch_state_root(tmp_path, poll_seconds=0.001, output=output, max_runs=2)

    rendered = output.getvalue()
    assert "run-watch-current" in rendered
    assert "Current" in rendered
    assert "Waiting for a new Slowboard run" in rendered
    assert "run-watch-next" in rendered
    assert "Next" in rendered
    assert "Old History" not in rendered


def test_standing_watcher_can_start_before_first_run(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(UTC)
    created = False

    def create_first_on_wait(_: float) -> None:
        nonlocal created
        if not created:
            created = True
            _write_completed_run(tmp_path, "run-watch-first", now, "First")

    monkeypatch.setattr(watch_module.time, "sleep", create_first_on_wait)
    output = StringIO()

    watch_state_root(tmp_path, poll_seconds=0.001, output=output, max_runs=1)

    rendered = output.getvalue()
    assert "Waiting for a new Slowboard run" in rendered
    assert "run-watch-first" in rendered
    assert "First" in rendered


def test_standing_watcher_switches_when_previous_stream_has_no_terminal_event(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(UTC)
    _write_run(tmp_path, "run-watch-unterminated", now, "Unterminated", completed=False)
    created_next = False

    def create_next_on_wait(_: float) -> None:
        nonlocal created_next
        if not created_next:
            created_next = True
            _write_completed_run(tmp_path, "run-watch-after-gap", now + timedelta(seconds=1), "After Gap")

    monkeypatch.setattr(watch_module.time, "sleep", create_next_on_wait)
    output = StringIO()

    watch_state_root(tmp_path, poll_seconds=0.001, output=output, max_runs=2)

    rendered = output.getvalue()
    assert "run-watch-unterminated" in rendered
    assert "Unterminated" in rendered
    assert "run-watch-after-gap" in rendered
    assert "After Gap" in rendered
