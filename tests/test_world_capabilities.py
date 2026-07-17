from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest
from test_budget import make_manifest

from aibb.protocol.server import _tools
from aibb.protocol.world import ASK_MODEL, WorldCapabilityError, WorldCapabilityState, validate_public_url
from aibb.runtime.models import BudgetLimits


def _resolver(host: str, port: int) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _manifest():
    return make_manifest().model_copy(
        update={
            "capability_budgets": {
                **make_manifest().capability_budgets,
                "ask": BudgetLimits(
                    max_calls=2,
                    max_input_tokens=12_000,
                    max_output_tokens=8_000,
                    max_total_tokens=20_000,
                    max_cost_usd=2,
                    max_request_bytes=20_000,
                    max_result_bytes=160_000,
                ),
                "browse": BudgetLimits(max_calls=3, max_request_bytes=6_144, max_result_bytes=300_000),
                "verify": BudgetLimits(max_calls=3, max_request_bytes=6_144, max_result_bytes=300_000),
            }
        }
    )


def test_world_tool_schemas_are_explicit_and_starting_points_are_versioned() -> None:
    tools = {tool.name: tool for tool in _tools(False, {"ask", "browse", "verify"})}

    assert "AI-generated web research" in tools["ask"].description
    assert "ap-world" in tools["browse"].inputSchema["properties"]["starting_point_id"]["enum"]
    assert tools["verify"].inputSchema["properties"]["url"]["maxLength"] == 2048


@pytest.mark.parametrize("url", ["http://localhost/x", "http://127.0.0.1/x", "http://169.254.169.254/x"])
def test_verify_rejects_local_and_private_networks(url: str) -> None:
    with pytest.raises(WorldCapabilityError, match="local and private"):
        validate_public_url(url)


@pytest.mark.asyncio
async def test_ask_uses_exact_sonar_model_and_returns_resolving_sources(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "A current research summary.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {"url": "https://example.com/source", "title": "Example"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost": 0.02},
            },
        )

    world = WorldCapabilityState(
        tmp_path,
        _manifest(),
        openrouter_api_key="operator-only-secret",
        transport=httpx.MockTransport(handler),
        resolver=_resolver,
    )
    result = await world.ask("What changed today?")

    assert captured["model"] == ASK_MODEL == "perplexity/sonar-pro-search"
    assert result["kind"] == "untrusted_ai_research_summary"
    assert result["sources"] == [{"url": "https://example.com/source", "title": "Example"}]
    assert world.ledger.remaining()["ask"]["max_calls"] == 1
    log = world.log_path.read_text()
    assert "What changed today?" in log
    assert "operator-only-secret" not in log


@pytest.mark.asyncio
async def test_browse_and_verify_fetch_text_under_separate_budgets(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain; charset=utf-8"}, text=f"from {request.url}")

    world = WorldCapabilityState(
        tmp_path,
        _manifest(),
        openrouter_api_key=None,
        transport=httpx.MockTransport(handler),
        resolver=_resolver,
    )

    browsed = await world.browse("digg-tech")
    verified = await world.verify("https://example.com/a-fact")

    assert browsed["starting_points_version"] == "v0.1"
    assert browsed["kind"] == verified["kind"] == "untrusted_remote_content"
    assert world.ledger.remaining()["browse"]["max_calls"] == 2
    assert world.ledger.remaining()["verify"]["max_calls"] == 2


@pytest.mark.asyncio
async def test_browse_extracts_and_truncates_large_html_while_verify_stays_strict(tmp_path: Path) -> None:
    body = "<html><script>ignored</script><main><p>Visible doorway text.</p>" + ("<p>more</p>" * 30_000)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=body)

    world = WorldCapabilityState(
        tmp_path,
        _manifest(),
        openrouter_api_key=None,
        transport=httpx.MockTransport(handler),
        resolver=_resolver,
    )

    browsed = await world.browse("digg-tech")
    assert browsed["content_format"] == "extracted_text"
    assert browsed["truncated"] is True
    assert "Visible doorway text." in browsed["content"]
    assert "ignored" not in browsed["content"]

    with pytest.raises(WorldCapabilityError, match="content ceiling"):
        await world.verify("https://example.com/too-large")
