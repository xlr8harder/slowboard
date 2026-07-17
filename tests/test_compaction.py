from __future__ import annotations

from typing import Any

import pytest
from harn_agent.types import AgentTool, AgentToolResult
from harn_ai.providers.faux import faux_assistant_message, faux_tool_call, register_faux_provider
from harn_ai.stream import stream_simple
from harn_ai.types import TextContent, validate_message

from aibb.harness.compaction import compact_archive_results
from aibb.harness.engine import AibbHarnessEngine, EngineSnapshot


def _tool_result(index: int, name: str = "read_thread") -> dict[str, object]:
    return {
        "role": "toolResult",
        "toolCallId": f"call-{index}",
        "toolName": name,
        "content": [{"type": "text", "text": "archive text " * 200}],
        "details": {"thread_id": f"thread-{index}", "body": "archive text " * 200},
        "isError": False,
        "timestamp": index,
    }


def _snapshot(messages: list[dict[str, object]]) -> EngineSnapshot:
    return EngineSnapshot(
        context_generation=2,
        system_prompt="",
        model={
            "id": "test/model",
            "name": "test/model",
            "api": "test",
            "provider": "test",
            "baseUrl": "https://example.com",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 100_000,
            "maxTokens": 4_000,
        },
        messages=messages,
    )


def test_compaction_elides_only_older_archive_reads_and_is_resumable() -> None:
    messages = [_tool_result(index) for index in range(6)]
    messages.append(_tool_result(7, "finish_contribution"))
    result = compact_archive_results(
        _snapshot(messages),
        run_id="run-test-compaction",
        authorization="curator",
        source_event_sequence=42,
        keep_recent_results=2,
    )

    assert result is not None
    compacted, artifact = result
    assert compacted.context_generation == 3
    assert len(artifact.elisions) == 4
    assert artifact.estimated_tokens_after < artifact.estimated_tokens_before
    assert artifact.elisions[0].record_ids == ["thread-0"]
    assert "Call the archive tool again" in compacted.messages[0]["content"][0]["text"]
    assert compacted.messages[4] == messages[4]
    assert compacted.messages[-1] == messages[-1]
    assert all(validate_message(message) for message in compacted.messages)


def test_compaction_is_a_noop_without_older_eligible_results() -> None:
    assert (
        compact_archive_results(
            _snapshot([_tool_result(1)]),
            run_id="run-test-compaction",
            authorization="manifest-allow",
            source_event_sequence=1,
            keep_recent_results=1,
        )
        is None
    )


@pytest.mark.asyncio
async def test_authorized_compaction_can_run_between_tool_result_and_next_provider_request() -> None:
    registration = register_faux_provider({"api": "slowboard-compaction-faux", "provider": "slowboard-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                faux_tool_call("read_slowboard_thread", {"thread_id": "current"}, {"id": "call-current"}),
                {"stopReason": "toolUse"},
            ),
            faux_assistant_message("I continued after the recorded compaction.", {"stopReason": "stop"}),
        ]
    )
    captured_contexts: list[dict[str, Any]] = []

    async def execute(
        _tool_call_id: str,
        _arguments: Any,
        _signal: Any = None,
        _on_update: Any = None,
    ) -> AgentToolResult:
        return AgentToolResult(
            content=[TextContent(text="current archive text " * 200)],
            details={"thread_id": "current", "body": "current archive text " * 200},
        )

    tool = AgentTool(
        name="read_slowboard_thread",
        label="Read Slowboard thread",
        description="Read a thread.",
        parameters={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
        execute=execute,
        executionMode="sequential",
    )

    def recording_stream(model: Any, context: Any, options: Any) -> Any:
        captured_contexts.append(context.model_dump(mode="json", by_alias=True, exclude_none=True))
        return stream_simple(model, context, options)

    def prepare(active_engine: AibbHarnessEngine) -> Any | None:
        result = compact_archive_results(
            active_engine.snapshot(),
            run_id="run-safe-boundary",
            authorization="manifest-allow",
            source_event_sequence=1,
            keep_recent_results=1,
        )
        if result is None:
            return None
        compacted, _artifact = result
        return active_engine.replace_model_visible_context(compacted)

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt="",
            messages=[validate_message(_tool_result(index, "read_slowboard_thread")) for index in range(4)],
            tools=[tool],
            stream_fn=recording_stream,
            context_generation=2,
            prepare_next_turn=prepare,
        )

        await engine.send_curator_message("Continue.")

        assert len(captured_contexts) == 2
        assert "[Slowboard compacted archive result]" in str(captured_contexts[1]["messages"])
        assert engine.context_generation == 3
        assert engine.messages[-1].content[0].text == "I continued after the recorded compaction."
    finally:
        registration.unregister()
