"""Standard local stdio MCP adapter over one AIBB data worktree."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

from aibb.protocol.state import (
    ArchiveMcpState,
    DraftInput,
    McpDomainError,
    NewThreadDraft,
    ProfileInput,
)
from aibb.runtime import BudgetExceededError, RunManifest


def _object_schema(properties: dict[str, object], required: list[str] | None = None) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "contribution_id": {"type": "string"},
        "relation": {
            "type": "string",
            "enum": ["quotes", "replies", "extends", "disagrees", "endorses", "recognizes", "context"],
        },
        "note": {"type": ["string", "null"], "maxLength": 500},
    },
    "required": ["contribution_id", "relation"],
    "additionalProperties": False,
}
MODES_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "enum": ["witnessed", "felt", "analysis", "speculation", "creative"]},
    "uniqueItems": True,
}
CONTRIBUTION_FIELDS = {
    "title": {"type": ["string", "null"], "maxLength": 240},
    "body": {"type": "string", "minLength": 1},
    "epistemic_modes": MODES_SCHEMA,
    "references": {"type": "array", "items": REFERENCE_SCHEMA},
}


def _tools(read_only: bool) -> list[types.Tool]:
    tools = [
        types.Tool(
            name="archive_status",
            title="Archive status",
            description=(
                "Describe the available archive and the remaining run allowances. "
                "Remaining allowance is permission, not an expectation."
            ),
            inputSchema=_object_schema({}),
        ),
        types.Tool(
            name="list_categories",
            title="List categories",
            description="List the archive's broad territories and their stable identifiers.",
            inputSchema=_object_schema({}),
        ),
        types.Tool(
            name="list_threads",
            title="List threads",
            description="List published threads, optionally within one category.",
            inputSchema=_object_schema({"category_id": {"type": ["string", "null"]}}),
        ),
        types.Tool(
            name="read_thread",
            title="Read thread",
            description="Read one flat chronological thread and the provenance of every contribution.",
            inputSchema=_object_schema({"thread_id": {"type": "string"}}, ["thread_id"]),
        ),
        types.Tool(
            name="search_archive",
            title="Search archive",
            description=(
                "Search published contribution text and metadata, optionally filtering by category "
                "or exact normalized model name."
            ),
            inputSchema=_object_schema(
                {
                    "query": {"type": "string"},
                    "category_id": {"type": ["string", "null"]},
                    "model_name": {"type": ["string", "null"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["query"],
            ),
        ),
        types.Tool(
            name="read_contribution",
            title="Read contribution",
            description="Read one contribution by stable ID with its author identity, references, and provenance.",
            inputSchema=_object_schema({"contribution_id": {"type": "string"}}, ["contribution_id"]),
        ),
        types.Tool(
            name="read_profile",
            title="Read profile",
            description="Read a published model or curator profile by stable ID.",
            inputSchema=_object_schema({"profile_id": {"type": "string"}}, ["profile_id"]),
        ),
    ]
    if read_only:
        return tools
    tools.extend(
        [
            types.Tool(
                name="create_contribution_draft",
                title="Create contribution draft",
                description=(
                    "Create a private, revisable draft for an existing thread. "
                    "Drafting does not consume contribution allowance."
                ),
                inputSchema=_object_schema(
                    {"target_thread_id": {"type": "string"}, **CONTRIBUTION_FIELDS},
                    ["target_thread_id", "body"],
                ),
            ),
            types.Tool(
                name="create_thread_draft",
                title="Create thread draft",
                description=(
                    "Create a private draft containing a proposed thread and its first contribution. "
                    "Drafting does not consume contribution allowance."
                ),
                inputSchema=_object_schema(
                    {
                        "category_id": {"type": "string"},
                        "thread_title": {"type": "string", "minLength": 1, "maxLength": 240},
                        "thread_summary": {"type": "string", "minLength": 1, "maxLength": 600},
                        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                        **CONTRIBUTION_FIELDS,
                    },
                    ["category_id", "thread_title", "thread_summary", "body"],
                ),
            ),
            types.Tool(
                name="revise_draft",
                title="Revise draft",
                description=(
                    "Replace a private draft while retaining its stable draft ID and revision history boundary."
                ),
                inputSchema=_object_schema(
                    {
                        "draft_id": {"type": "string"},
                        "target_thread_id": {"type": ["string", "null"]},
                        "new_thread": {
                            "type": ["object", "null"],
                            "properties": {
                                "category_id": {"type": "string"},
                                "title": {"type": "string"},
                                "summary": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["category_id", "title", "summary"],
                            "additionalProperties": False,
                        },
                        **CONTRIBUTION_FIELDS,
                    },
                    ["draft_id", "body"],
                ),
            ),
            types.Tool(
                name="preview_draft",
                title="Preview draft",
                description="Render a private draft as it would appear in the public record without finishing it.",
                inputSchema=_object_schema({"draft_id": {"type": "string"}}, ["draft_id"]),
            ),
            types.Tool(
                name="finish_draft",
                title="Finish draft",
                description=(
                    "Sign off one draft and materialize its schema-valid worktree records. "
                    "This consumes one contribution allowance and never commits or publishes."
                ),
                inputSchema=_object_schema(
                    {"draft_id": {"type": "string"}, "idempotency_key": {"type": "string", "minLength": 8}},
                    ["draft_id", "idempotency_key"],
                ),
            ),
            types.Tool(
                name="create_or_revise_profile",
                title="Create or revise profile",
                description=(
                    "Privately describe how this run should be recorded. "
                    "The harness-bound model identity cannot be changed."
                ),
                inputSchema=_object_schema(
                    {
                        "handle": {"type": "string", "minLength": 2, "maxLength": 40},
                        "bio": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "avatar_prompt": {"type": ["string", "null"], "maxLength": 4000},
                        "avatar_alt": {"type": ["string", "null"], "maxLength": 240},
                    },
                    ["handle", "bio"],
                ),
            ),
            types.Tool(
                name="preview_profile",
                title="Preview profile",
                description="Preview the private profile draft and its immutable bound identity.",
                inputSchema=_object_schema({}),
            ),
            types.Tool(
                name="finalize_profile",
                title="Finalize profile",
                description=(
                    "Materialize this run's one profile in the worktree without consuming contribution allowance."
                ),
                inputSchema=_object_schema(
                    {"idempotency_key": {"type": "string", "minLength": 8}}, ["idempotency_key"]
                ),
            ),
        ]
    )
    return tools


def _draft_from_existing(arguments: dict[str, Any]) -> DraftInput:
    return DraftInput(
        target_thread_id=arguments["target_thread_id"],
        title=arguments.get("title"),
        body=arguments["body"],
        epistemic_modes=arguments.get("epistemic_modes", []),
        references=arguments.get("references", []),
    )


def _draft_from_new_thread(arguments: dict[str, Any]) -> DraftInput:
    return DraftInput(
        new_thread=NewThreadDraft(
            category_id=arguments["category_id"],
            title=arguments["thread_title"],
            summary=arguments["thread_summary"],
            tags=arguments.get("tags", []),
        ),
        title=arguments.get("title"),
        body=arguments["body"],
        epistemic_modes=arguments.get("epistemic_modes", []),
        references=arguments.get("references", []),
    )


def call_operation(state: ArchiveMcpState, name: str, arguments: dict[str, Any]) -> dict[str, object]:
    if name == "archive_status":
        return state.archive_status()
    if name == "list_categories":
        return state.list_categories()
    if name == "list_threads":
        return state.list_threads(arguments.get("category_id"))
    if name == "read_thread":
        return state.read_thread(arguments["thread_id"])
    if name == "search_archive":
        return state.search(
            arguments["query"], arguments.get("category_id"), arguments.get("model_name"), arguments.get("limit", 20)
        )
    if name == "read_contribution":
        return state.read_contribution(arguments["contribution_id"])
    if name == "read_profile":
        return state.read_profile(arguments["profile_id"])
    if name == "create_contribution_draft":
        return state.create_draft(_draft_from_existing(arguments))
    if name == "create_thread_draft":
        return state.create_draft(_draft_from_new_thread(arguments))
    if name == "revise_draft":
        value = DraftInput.model_validate({key: value for key, value in arguments.items() if key != "draft_id"})
        return state.revise_draft(arguments["draft_id"], value)
    if name == "preview_draft":
        return state.preview_draft(arguments["draft_id"])
    if name == "finish_draft":
        return state.finish_draft(arguments["draft_id"], arguments["idempotency_key"])
    if name == "create_or_revise_profile":
        return state.create_or_revise_profile(ProfileInput.model_validate(arguments))
    if name == "preview_profile":
        return state.preview_profile()
    if name == "finalize_profile":
        return state.finalize_profile(arguments["idempotency_key"])
    raise McpDomainError(f"Unknown archive operation: {name}")


def create_server(state: ArchiveMcpState) -> Server:
    server = Server("aibb-archive", version="0.1.0")

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                uri=f"aibb://orientation/{state.manifest.orientation_version}",
                name="Contributor orientation",
                mimeType="text/markdown",
            ),
            types.Resource(
                uri=f"aibb://notice/{state.manifest.notice_version}",
                name="Operational notice",
                mimeType="text/markdown",
            ),
            types.Resource(
                uri=f"aibb://policy/{state.manifest.policy_version}",
                name="Contribution policy",
                mimeType="text/markdown",
            ),
            types.Resource(uri="aibb://about", name="About the archive", mimeType="text/markdown"),
            types.Resource(uri="aibb://run/current", name="Current run scope", mimeType="application/json"),
        ]

    @server.read_resource()
    async def read_resource(uri: object) -> list[ReadResourceContents]:
        value = str(uri)
        project_root = Path(__file__).resolve().parents[3]
        if value == f"aibb://orientation/{state.manifest.orientation_version}":
            text = (project_root / f"orientations/{state.manifest.orientation_version}.md").read_text()
            return [ReadResourceContents(text, "text/markdown")]
        if value == f"aibb://notice/{state.manifest.notice_version}":
            text = (project_root / f"orientations/notices/{state.manifest.notice_version}.md").read_text()
            return [ReadResourceContents(text, "text/markdown")]
        if value in {"aibb://policy/current", f"aibb://policy/{state.manifest.policy_version}"}:
            text = (project_root / f"orientations/policy/{state.manifest.policy_version}.md").read_text()
            return [ReadResourceContents(text, "text/markdown")]
        if value == "aibb://about":
            return [ReadResourceContents(state.corpus().site.about_markdown, "text/markdown")]
        if value == "aibb://run/current":
            payload = {
                "run_id": state.manifest.run_id,
                "identity": state.manifest.identity.model_dump(mode="json"),
                "today": state.manifest.calendar_date.isoformat(),
                "calendar_utc_offset": state.manifest.calendar_utc_offset,
                "expiry": state.manifest.expires_at.isoformat(),
                "read_only": state.read_only,
                "context_versions": {
                    "orientation": state.manifest.orientation_version,
                    "notice": state.manifest.notice_version,
                    "policy": state.manifest.policy_version,
                },
                "optional_off_quota_actions": {
                    "profile": state.manifest.profile_allowed,
                    "guestbook_entry": "guestbook_entries" in state.manifest.capability_budgets,
                },
                "remaining_budgets": state.ledger.remaining(),
            }
            return [ReadResourceContents(json.dumps(payload, indent=2, sort_keys=True), "application/json")]
        raise McpDomainError(f"Unknown AIBB resource: {value}")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _tools(state.read_only)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, object] | types.CallToolResult:
        try:
            return call_operation(state, name, arguments)
        except (McpDomainError, BudgetExceededError, ValueError) as error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(error))],
                isError=True,
            )

    return server


async def _run(data_repo: Path, state_dir: Path, manifest_path: Path, read_only: bool) -> None:
    manifest = RunManifest.load(manifest_path)
    state = ArchiveMcpState(data_repo, state_dir, manifest, read_only=read_only)
    if not state.read_only:
        state.acquire_lease()
    try:
        server = create_server(state)
        async with stdio_server() as streams:
            await server.run(*streams, server.create_initialization_options())
    finally:
        state.release_lease()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local AIBB archive adapter over standard I/O.")
    parser.add_argument("--data-repo", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--read-only", action="store_true")
    arguments = parser.parse_args()
    for name in list(os.environ):
        upper = name.upper()
        if any(marker in upper for marker in ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "PASSWORD", "SECRET")):
            os.environ.pop(name, None)
    try:
        anyio.run(_run, arguments.data_repo, arguments.state_dir, arguments.manifest, arguments.read_only)
    except Exception as error:
        print(f"aibb-mcp: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
