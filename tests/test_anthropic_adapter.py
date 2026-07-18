from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from harn_ai.types import AssistantMessage, Context, DoneEvent, StartEvent, Usage, UsageCost
from harn_ai.utils.event_stream import AssistantMessageEventStream
from test_budget import make_manifest

from aibb.harness.anthropic import ANTHROPIC_ENDPOINT, AnthropicAdapter, anthropic_model
from aibb.runtime import BudgetLedger
from aibb.sessions import SessionStore


def test_anthropic_catalog_keeps_claude_3_opus_limits_and_modalities() -> None:
    model = anthropic_model("claude-3-opus-20240229")

    assert model.provider == "anthropic"
    assert model.api == "anthropic-messages"
    assert model.contextWindow == 200_000
    assert model.maxTokens == 4_096
    assert model.reasoning is False
    assert model.input == ["text", "image"]


@pytest.mark.asyncio
async def test_anthropic_adapter_uses_native_stream_with_budgeted_private_capture(tmp_path: Path) -> None:
    native_requests: list[dict[str, Any]] = []

    def fake_native_stream(model: Any, _context: Any, options: dict[str, Any]) -> AssistantMessageEventStream:
        native = AssistantMessageEventStream()

        async def emit() -> None:
            payload = await options["onPayload"](
                {
                    "model": model.id,
                    "messages": [{"role": "user", "content": "Explore."}],
                    "max_tokens": 4_096,
                    "stream": True,
                },
                model,
            )
            native_requests.append({"payload": payload, "options": options})
            await options["onResponse"](
                {"status": 200, "headers": {"request-id": "req-test", "x-secret": "omit"}},
                model,
            )
            usage = Usage(
                input=120,
                output=30,
                cacheRead=0,
                cacheWrite=0,
                totalTokens=150,
                cost=UsageCost(input=0.0018, output=0.00225, cacheRead=0, cacheWrite=0, total=0.00405),
            )
            output = AssistantMessage(
                content=[],
                api=model.api,
                provider=model.provider,
                model=model.id,
                responseId="msg-test",
                usage=usage,
                stopReason="stop",
                timestamp=1,
            )
            native.push(StartEvent(partial=output))
            native.push(DoneEvent(reason="stop", message=output))
            native.end()

        asyncio.create_task(emit())
        return native

    manifest = make_manifest().model_copy(
        update={
            "identity": make_manifest().identity.model_copy(
                update={
                    "provider": "anthropic",
                    "endpoint": ANTHROPIC_ENDPOINT,
                    "model_name": "claude-3-opus-20240229",
                    "normalized_model_name": "claude-3-opus-20240229",
                }
            )
        }
    )
    ledger = BudgetLedger(tmp_path / "mcp/budgets.json", manifest)
    session = SessionStore(tmp_path / "session", manifest.run_id)
    adapter = AnthropicAdapter(
        api_key="private-anthropic-key",
        ledger=ledger,
        session=session,
        max_output_tokens=500,
        tool_choice="required",
        stream_fn=fake_native_stream,
    )

    events = [
        event
        async for event in adapter(
            anthropic_model("claude-3-opus-20240229"),
            Context(systemPrompt="", messages=[], tools=[]),
            None,
        )
    ]

    assert events[-1].type == "done"
    assert native_requests[0]["payload"]["max_tokens"] == 500
    assert native_requests[0]["options"]["toolChoice"] == "any"
    assert native_requests[0]["options"]["cacheRetention"] == "none"
    inference = ledger.read().accounts["inference"]
    assert inference.used.calls == 1
    assert inference.used.total_tokens == 150
    event_text = (tmp_path / "session/events.jsonl").read_text()
    assert "private-anthropic-key" not in event_text
    assert "req-test" in event_text
    assert "x-secret" not in event_text
    assert ANTHROPIC_ENDPOINT in event_text
