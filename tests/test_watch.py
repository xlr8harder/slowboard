from __future__ import annotations

import json
from io import StringIO

from rich.console import Console

from aibb.harness.watch import RunEventRenderer


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
    assert "run completed · model_concluded_visit" in rendered
