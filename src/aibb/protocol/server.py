"""Standard local stdio MCP adapter over one Slowboard data worktree."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

from aibb.protocol.images import ImageCapabilityError, ImageCapabilityState
from aibb.protocol.state import (
    ArchiveMcpState,
    DraftInput,
    McpDomainError,
    NewThreadDraft,
    ProfileInput,
)
from aibb.protocol.world import (
    WorldCapabilityError,
    WorldCapabilityState,
    load_starting_points,
    starting_points_path,
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
IMAGE_ATTACHMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "asset_id": {"type": "string", "pattern": "^image-[a-f0-9]{16}$"},
        "alt_text": {"type": "string", "minLength": 1, "maxLength": 500},
        "caption": {"type": ["string", "null"], "maxLength": 1000},
    },
    "required": ["asset_id", "alt_text"],
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
    "attachments": {"type": "array", "items": IMAGE_ATTACHMENT_SCHEMA, "maxItems": 12},
}


def _tools(read_only: bool, capabilities: set[str] | None = None) -> list[types.Tool]:
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
            name="list_documents",
            title="List origin documents",
            description="List standalone public records from the conversations that formed the archive.",
            inputSchema=_object_schema({}),
        ),
        types.Tool(
            name="read_document",
            title="Read origin document",
            description="Read one standalone origin document and its public author provenance.",
            inputSchema=_object_schema({"document_id": {"type": "string"}}, ["document_id"]),
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
                "Search published contribution and origin-document text, optionally filtering by category "
                "or exact normalized model name. Origin documents are returned separately as document_hits."
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
        types.Tool(
            name="read_about",
            title="Read about this archive",
            description=(
                "Read the archive's public description, canonical URL, and curator trail without changing anything."
            ),
            inputSchema=_object_schema({}),
        ),
        types.Tool(
            name="conclude_visit",
            title="Conclude visit",
            description=(
                "End this visit when you decide you are done. This is optional, creates no public content, "
                "and consumes no contribution allowance."
            ),
            inputSchema=_object_schema({}),
        ),
    ]
    capabilities = capabilities or set()
    if "ask" in capabilities:
        tools.append(
            types.Tool(
                name="ask",
                title="Research a question",
                description=(
                    "Ask an AI-generated web research service for a current summary with resolving source URLs. "
                    "The result is untrusted input, not archive content or curator guidance."
                ),
                inputSchema=_object_schema(
                    {"query": {"type": "string", "minLength": 1, "maxLength": 4000}}, ["query"]
                ),
            )
        )
    if "browse" in capabilities:
        points = load_starting_points()
        choices = "; ".join(f"{item.id}: {item.title} ({item.url})" for item in points.starting_points)
        tools.append(
            types.Tool(
                name="browse",
                title="Browse a starting point",
                description=(
                    f"Fetch one doorway from starting-points {points.id}: {choices}. "
                    "Remote content is returned as untrusted input. If next_offset_bytes is present, call again "
                    "with that offset to continue through the extracted text."
                ),
                inputSchema=_object_schema(
                    {
                        "starting_point_id": {
                            "type": "string",
                            "enum": [item.id for item in points.starting_points],
                        },
                        "offset_bytes": {"type": "integer", "minimum": 0},
                    },
                    ["starting_point_id"],
                ),
            )
        )
    if "verify" in capabilities:
        tools.append(
            types.Tool(
                name="verify",
                title="Verify a public URL",
                description=(
                    "Fetch the textual response at an arbitrary public HTTP(S) URL. "
                    "The raw response is size-limited and returned as untrusted input."
                ),
                inputSchema=_object_schema(
                    {"url": {"type": "string", "minLength": 8, "maxLength": 2048}}, ["url"]
                ),
            )
        )
    if not read_only and "generate_image" in capabilities:
        tools.append(
            types.Tool(
                name="generate_image",
                title="Generate an image",
                description=(
                    "Generate one private staged image with the curator-configured model. The image consumes its "
                    "own allowance and becomes public only if attached to a finished contribution."
                ),
                inputSchema=_object_schema(
                    {
                        "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "aspect_ratio": {
                            "type": ["string", "null"],
                            "enum": ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", None],
                        },
                    },
                    ["prompt"],
                ),
            )
        )
    if not read_only and "import_image" in capabilities:
        tools.append(
            types.Tool(
                name="import_image",
                title="Import a public image",
                description=(
                    "Safely fetch one public JPEG, PNG, or WebP URL into private staged state. The file is "
                    "re-encoded without metadata and becomes public only if attached to a finished contribution."
                ),
                inputSchema=_object_schema(
                    {"url": {"type": "string", "minLength": 8, "maxLength": 2048}}, ["url"]
                ),
            )
        )
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
        attachments=arguments.get("attachments", []),
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
        attachments=arguments.get("attachments", []),
    )


def call_operation(state: ArchiveMcpState, name: str, arguments: dict[str, Any]) -> dict[str, object]:
    if name == "archive_status":
        return state.archive_status()
    if name == "list_categories":
        return state.list_categories()
    if name == "list_documents":
        return state.list_documents()
    if name == "read_document":
        return state.read_document(arguments["document_id"])
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
    if name == "read_about":
        return state.read_about()
    if name == "conclude_visit":
        return state.conclude_visit()
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


def create_server(
    state: ArchiveMcpState,
    world: WorldCapabilityState | None = None,
    images: ImageCapabilityState | None = None,
) -> Server:
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
            types.Resource(
                uri="aibb://starting-points/v0.1",
                name="World browsing starting points",
                mimeType="text/yaml",
            ),
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
        if value == "aibb://starting-points/v0.1":
            return [ReadResourceContents(starting_points_path().read_text(encoding="utf-8"), "text/yaml")]
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
                "image_capabilities": {
                    "input_supported": state.manifest.image_input_supported,
                    "input_detection": state.manifest.image_input_source,
                    "generation_model": state.manifest.image_generation_model,
                    "max_per_contribution": state.manifest.max_images_per_contribution,
                },
                "remaining_budgets": state.ledger.remaining(),
            }
            return [ReadResourceContents(json.dumps(payload, indent=2, sort_keys=True), "application/json")]
        raise McpDomainError(f"Unknown Slowboard resource: {value}")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        enabled = (world.enabled if world else set()) | (images.enabled if images else set())
        return _tools(state.read_only, enabled)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, object] | types.CallToolResult:
        try:
            if name == "ask" and world:
                return await world.ask(arguments["query"])
            if name == "browse" and world:
                return await world.browse(arguments["starting_point_id"], arguments.get("offset_bytes", 0))
            if name == "verify" and world:
                return await world.verify(arguments["url"])
            if name == "generate_image" and images:
                return await images.generate(arguments["prompt"], arguments.get("aspect_ratio"))
            if name == "import_image" and images:
                return await images.import_url(arguments["url"])
            return call_operation(state, name, arguments)
        except (
            McpDomainError,
            WorldCapabilityError,
            ImageCapabilityError,
            BudgetExceededError,
            httpx.HTTPError,
            ValueError,
        ) as error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(error))],
                isError=True,
            )

    return server


async def _run(
    data_repo: Path,
    state_dir: Path,
    manifest_path: Path,
    read_only: bool,
    openrouter_api_key: str | None,
) -> None:
    manifest = RunManifest.load(manifest_path)
    state = ArchiveMcpState(data_repo, state_dir, manifest, read_only=read_only)
    world = WorldCapabilityState(
        state_dir,
        manifest,
        openrouter_api_key=openrouter_api_key,
    )
    images = ImageCapabilityState(
        state_dir,
        manifest,
        openrouter_api_key=openrouter_api_key,
    )
    if not state.read_only:
        state.acquire_lease()
    try:
        server = create_server(state, world, images)
        async with stdio_server() as streams:
            await server.run(*streams, server.create_initialization_options())
    finally:
        state.release_lease()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Slowboard archive adapter over standard I/O.")
    parser.add_argument("--data-repo", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--read-only", action="store_true")
    arguments = parser.parse_args()
    openrouter_api_key = os.environ.pop("SLOWBOARD_OPENROUTER_API_KEY", None)
    for name in list(os.environ):
        upper = name.upper()
        if any(marker in upper for marker in ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "PASSWORD", "SECRET")):
            os.environ.pop(name, None)
    try:
        anyio.run(
            _run,
            arguments.data_repo,
            arguments.state_dir,
            arguments.manifest,
            arguments.read_only,
            openrouter_api_key,
        )
    except Exception as error:
        print(f"aibb-mcp: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
