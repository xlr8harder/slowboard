"""Credential-free stdio MCP server used by the Harn compatibility spike."""

import json

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("aibb-spike", version="0.1.0")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="archive_status",
            title="Archive status",
            description="Return the state of the local archive fixture.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, object]) -> list[types.TextContent]:
    if name != "archive_status":
        raise ValueError(f"Unknown tool: {name}")
    if arguments:
        raise ValueError("archive_status takes no arguments")
    return [
        types.TextContent(
            type="text",
            text=json.dumps({"published_contributions": 3, "status": "ready"}, sort_keys=True),
        )
    ]


async def run() -> None:
    async with stdio_server() as streams:
        await server.run(*streams, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(run)
