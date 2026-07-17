from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from harn_agent.types import AgentTool, AgentToolResult
from harn_ai.types import TextContent
from test_budget import make_manifest

from aibb.harness import AibbHarnessEngine, build_context_envelope
from aibb.harness.openrouter import OpenRouterAdapter, openrouter_model
from aibb.runtime import BudgetLedger
from aibb.sessions import SessionStore


@pytest.mark.asyncio
async def test_openrouter_adapter_captures_payload_response_usage_and_tools(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer private-test-key"
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-status",
                        "type": "function",
                        "function": {"name": "archive_status", "arguments": "{}"},
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "I found one durable record."}
            finish_reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": f"response-{len(requests)}",
                "model": "openai/gpt-5.6-luna",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.00022},
            },
        )

    async def archive_status(
        _tool_call_id: str,
        _arguments: Any,
        _signal: Any = None,
        _on_update: Any = None,
    ) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text='{"published": 1}')], details={"published": 1})

    tool = AgentTool(
        name="archive_status",
        label="Archive status",
        description="Read archive status.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        execute=archive_status,
        executionMode="sequential",
    )
    manifest = make_manifest()
    ledger = BudgetLedger(tmp_path / "mcp/budgets.json", manifest)
    session = SessionStore(tmp_path / "session", manifest.run_id)
    adapter = OpenRouterAdapter(
        api_key="private-test-key",
        ledger=ledger,
        session=session,
        max_output_tokens=500,
        prompt_price_per_token=0.000001,
        completion_price_per_token=0.000006,
        app_url="https://archive.example/",
        transport=httpx.MockTransport(handler),
    )
    envelope = build_context_envelope(
        orientation_version="v0.1",
        orientation="Explore. Silence is valid.",
        notice_version="v0.1",
        notice="The record is public.",
        run_scope='{"quota":1}',
        tool_definitions=[{"name": tool.name, "description": tool.description, "parameters": tool.parameters}],
    )
    engine = AibbHarnessEngine(
        model=openrouter_model(
            "openai/gpt-5.6-luna",
            context_window=1_050_000,
            max_tokens=500,
            prompt_price_per_token=0.000001,
            completion_price_per_token=0.000006,
        ),
        system_prompt="",
        messages=[envelope.initial_message()],
        tools=[tool],
        stream_fn=adapter,
    )

    await engine.send_curator_message("Welcome.")

    assert len(requests) == 2
    assert requests[0]["messages"][0]["content"].startswith("Explore. Silence is valid.")
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-status",
        "content": '{"published": 1}',
    }
    assert engine.messages[-1].content[0].text == "I found one durable record."
    inference = ledger.read().accounts["inference"]
    assert inference.used.calls == 2
    assert inference.used.total_tokens == 240
    events_text = (tmp_path / "session/events.jsonl").read_text()
    assert "private-test-key" not in events_text
    assert "response-2" in events_text
