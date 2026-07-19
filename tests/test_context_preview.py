from __future__ import annotations

from aibb.harness.context_preview import render_run_context


def test_context_preview_preserves_intro_compaction_receipt_and_tools() -> None:
    context = {
        "run_id": "run-preview",
        "checkpoint_event_sequence": 12,
        "context_generation": 1,
        "estimated_message_tokens": 321,
        "system_prompt": "",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Original Slowboard introduction."}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I should read."},
                    {
                        "type": "toolCall",
                        "id": "call-one",
                        "name": "read_slowboard_thread",
                        "arguments": {"thread_id": "thread-one"},
                    },
                ],
            },
            {
                "role": "toolResult",
                "toolName": "read_slowboard_thread",
                "toolCallId": "call-one",
                "content": [
                    {
                        "type": "text",
                        "text": "[Slowboard compacted archive result]\nrecord_ids: contribution-one",
                    }
                ],
            },
        ],
        "tools": [
            {
                "name": "read_slowboard_thread",
                "description": "Read one thread.",
                "parameters": {"type": "object"},
            }
        ],
        "compactions": [{"elided_results": 1}],
    }

    rendered = render_run_context(context)

    assert "Original Slowboard introduction." in rendered
    assert "[Slowboard compacted archive result]" in rendered
    assert "I should read." in rendered
    assert "tool call read_slowboard_thread id=call-one" in rendered
    assert "AVAILABLE TOOLS" in rendered
    assert "compaction_count: 1" in rendered
    assert "[empty: this run has no custom system prompt]" in rendered
