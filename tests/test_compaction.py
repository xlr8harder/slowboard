from __future__ import annotations

from harn_ai.types import validate_message

from aibb.harness.compaction import compact_archive_results
from aibb.harness.engine import EngineSnapshot


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
