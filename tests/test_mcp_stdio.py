from __future__ import annotations

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
        assert {"search_archive", "create_contribution_draft", "finish_draft"} <= tool_names
        resources = await session.list_resources()
        resource_uris = {str(resource.uri).rstrip("/") for resource in resources.resources}
        assert "aibb://policy/v0.1" in resource_uris
        status = await session.call_tool("archive_status", {})
        assert not status.isError
        assert status.structuredContent["remaining_budgets"]["contributions"]["max_calls"] == 1
        policy = await session.read_resource("aibb://policy/v0.1")
        assert "Silence is valid" in policy.contents[0].text
