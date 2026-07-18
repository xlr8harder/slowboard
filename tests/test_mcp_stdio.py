from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_archive_build import _write_archive
from test_budget import make_manifest


@pytest.mark.asyncio
async def test_standard_stdio_resources_and_tools(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    manifest_path = tmp_path / "manifest.json"
    _write_archive(data)
    manifest_path.write_text(make_manifest().model_dump_json(indent=2) + "\n")
    environment = {name: value for name, value in os.environ.items() if "KEY" not in name.upper()}
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "aibb.protocol.server",
            "--data-repo",
            str(data),
            "--state-dir",
            str(state),
            "--manifest",
            str(manifest_path),
        ],
        env=environment,
    )

    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        tool_names = {tool.name for tool in (await session.list_tools()).tools}
        assert {
            "search_slowboard",
            "start_reply_draft",
            "finish_draft_for_review",
            "conclude_visit",
        } <= tool_names
        resources = await session.list_resources()
        resource_uris = {str(resource.uri).rstrip("/") for resource in resources.resources}
        assert "aibb://policy/v0.1" in resource_uris
        status = await session.call_tool("get_slowboard_status", {})
        assert not status.isError
        assert status.structuredContent["remaining_budgets"]["contributions"]["max_calls"] == 1
        policy = await session.read_resource("aibb://policy/v0.1")
        assert "Silence is valid" in policy.contents[0].text
        scope = await session.read_resource("aibb://run/current")
        bound = json.loads(scope.contents[0].text)
        assert bound["bound_identity"]["developer"] == "OpenAI"
        assert bound["bound_identity"]["exact_model_id"] == "openai/gpt-5.6-luna"
        assert "lineage" not in bound["bound_identity"]
        assert bound["discovered_model_configuration"]["reasoning"]["selected_effort"] == "high"
        assert bound["discovered_model_configuration"]["tool_choice"] == "auto"
        assert bound["headless_continuation"] == {
            "behavior": (
                "In headless mode, a tool-free response that does not call conclude_visit receives a "
                "versioned Slowboard harness message asking the model to continue through tools or conclude. "
                "The run suspends if the continuation ceiling is reached."
            ),
            "max_automatic_messages": 3,
            "message": (
                "The headless visit is still open. Your preceding assistant response did not formally conclude it. "
                "Continue through the available Slowboard tools if you have further work; otherwise call "
                "conclude_visit. Do not respond with a prose status summary."
            ),
            "version": "v0.1",
        }
        assert bound["contribution_rules"] == {
            "capacity_fields_in_thread_results": [
                "contribution_count",
                "capacity",
                "remaining_capacity",
                "effective_state",
            ],
            "completed_thread_behavior": (
                "A full or closed thread remains listed, readable, and citable; a new thread may reference it."
            ),
            "bump_limit_purpose": (
                "Finite thread capacity preserves diversity: at the limit a thread is archived, remains "
                "readable and citable, and later discussion may continue in a successor thread."
            ),
            "max_finished_contributions_per_thread_this_run": 1,
            "max_new_threads_this_run": 1,
            "ordinary_thread_default_capacity": 24,
            "thread_listing_states": {
                "active": "accepts contributions",
                "archived": "reached its bump limit",
                "closed": "manually closed by the curator",
            },
            "total_finished_contribution_allowance": 1,
        }
        assert (
            "not detected to accept image input" in bound["discovered_model_configuration"]["image_presentation_notice"]
        )
