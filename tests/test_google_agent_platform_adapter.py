from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from harn_agent.types import AgentTool, AgentToolResult
from harn_ai.types import TextContent
from test_budget import make_manifest

from aibb.harness import AibbHarnessEngine
from aibb.harness.google_agent_platform import (
    GROK_4_1_FAST_CONTEXT_WINDOW,
    GROK_4_1_FAST_REASONING,
    GoogleAgentPlatformAdapter,
    google_agent_platform_endpoint,
    google_agent_platform_model,
)
from aibb.runtime import BudgetLedger
from aibb.sessions import SessionStore


def test_google_agent_platform_model_is_pinned_to_probed_reasoning_route() -> None:
    endpoint = google_agent_platform_endpoint(project_id="test-project")
    model = google_agent_platform_model(GROK_4_1_FAST_REASONING, endpoint=endpoint, max_tokens=16_000)

    assert endpoint.endswith("/projects/test-project/locations/global/endpoints/openapi/chat/completions")
    assert model.provider == "google_agent_platform"
    assert model.reasoning is True
    assert model.contextWindow == GROK_4_1_FAST_CONTEXT_WINDOW
    assert model.maxTokens == 16_000
    assert model.input == ["text", "image"]

    with pytest.raises(ValueError, match="Unsupported Google Agent Platform model"):
        google_agent_platform_model("xai/unknown", endpoint=endpoint, max_tokens=100)


@pytest.mark.asyncio
async def test_google_adapter_preserves_tool_calls_usage_and_tool_results(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []
    endpoint = google_agent_platform_endpoint(project_id="test-project")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == endpoint
        assert request.headers["x-goog-api-key"] == "private-google-key"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": "",
                "refusal": None,
                "tool_calls": [
                    {
                        "id": "call-status",
                        "type": "function",
                        "function": {"name": "archive_status", "arguments": "{}"},
                    }
                ],
            }
            finish_reason = "tool_calls"
            usage = {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 125,
                "completion_tokens_details": {"reasoning_tokens": 20},
                "prompt_tokens_details": {"cached_tokens": 90},
                "cost_in_usd_ticks": 0,
            }
        else:
            message = {"role": "assistant", "content": "The archive is open.", "refusal": None}
            finish_reason = "stop"
            usage = {
                "prompt_tokens": 110,
                "completion_tokens": 10,
                "total_tokens": 150,
                "completion_tokens_details": {"reasoning_tokens": 30},
                "prompt_tokens_details": {"cached_tokens": 95},
                "cost_in_usd_ticks": 0,
            }
        return httpx.Response(
            200,
            json={
                "id": f"google-response-{len(requests)}",
                "model": GROK_4_1_FAST_REASONING,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": usage,
            },
        )

    async def archive_status(
        _tool_call_id: str,
        _arguments: Any,
        _signal: Any = None,
        _on_update: Any = None,
    ) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text='{"published":1}')], details={"published": 1})

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
    adapter = GoogleAgentPlatformAdapter(
        api_key="private-google-key",
        endpoint=endpoint,
        ledger=ledger,
        session=session,
        max_output_tokens=500,
        transport=httpx.MockTransport(handler),
    )
    engine = AibbHarnessEngine(
        model=google_agent_platform_model(GROK_4_1_FAST_REASONING, endpoint=endpoint, max_tokens=500),
        system_prompt="",
        messages=[{"role": "user", "content": [{"type": "text", "text": "Inspect."}], "timestamp": 1}],
        tools=[tool],
        stream_fn=adapter,
    )

    await engine.begin()

    assert len(requests) == 2
    assert "reasoning" not in requests[0]
    assert requests[0]["model"] == GROK_4_1_FAST_REASONING
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-status",
        "content": '{"published":1}',
    }
    assert engine.messages[-1].content[0].text == "The archive is open."
    inference = ledger.read().accounts["inference"]
    assert inference.used.calls == 2
    assert inference.used.input_tokens == 210
    assert inference.used.output_tokens == 15
    assert inference.used.total_tokens == 275
    events_text = (tmp_path / "session/events.jsonl").read_text()
    assert "private-google-key" not in events_text
    assert '"reasoning_tokens":20' in events_text
