from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from harn_ai.types import AssistantMessage, Context, DoneEvent, StartEvent, Usage, UsageCost
from harn_ai.utils.event_stream import AssistantMessageEventStream
from test_budget import make_manifest

from aibb.harness.amazon_bedrock import (
    AmazonBedrockAdapter,
    _thinking_budget,
    amazon_bedrock_model,
    bedrock_credential_source,
    legacy_sonnet_base_id,
    probe_legacy_sonnet_availability,
)
from aibb.runtime import BudgetLedger
from aibb.runtime.models import BudgetLimits
from aibb.sessions import SessionStore


def test_legacy_sonnet_catalog_uses_exact_bedrock_ids_and_conservative_prices() -> None:
    model = amazon_bedrock_model(
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        region="us-east-1",
    )
    reasoning_model = amazon_bedrock_model(
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
        region="us-west-2",
        max_tokens=16_000,
    )

    assert model.provider == "amazon-bedrock"
    assert model.api == "bedrock-converse-stream"
    assert model.baseUrl == "https://bedrock-runtime.us-east-1.amazonaws.com"
    assert model.contextWindow == 200_000
    assert model.maxTokens == 8_192
    assert model.cost.input == 6
    assert model.cost.output == 30
    assert model.input == ["text", "image"]
    assert reasoning_model.id == "anthropic.claude-3-7-sonnet-20250219-v1:0"
    assert reasoning_model.reasoning is True
    assert reasoning_model.maxTokens == 16_000

    with pytest.raises(ValueError, match="base IDs reported by the availability probe"):
        amazon_bedrock_model("anthropic.claude-sonnet-4-20250514-v1:0", region="us-east-1")


def test_inference_profile_ids_resolve_to_legacy_specs_but_keep_the_profile_route() -> None:
    profile_model = amazon_bedrock_model(
        "apac.anthropic.claude-3-5-sonnet-20240620-v1:0",
        region="ap-south-1",
    )

    assert profile_model.id == "apac.anthropic.claude-3-5-sonnet-20240620-v1:0"
    assert profile_model.name == "Claude 3.5 Sonnet"
    assert profile_model.maxTokens == 8_192
    assert profile_model.baseUrl == "https://bedrock-runtime.ap-south-1.amazonaws.com"
    assert (
        legacy_sonnet_base_id("apac.anthropic.claude-3-5-sonnet-20240620-v1:0")
        == "anthropic.claude-3-5-sonnet-20240620-v1:0"
    )
    assert (
        legacy_sonnet_base_id("us.anthropic.claude-3-7-sonnet-20250219-v1:0")
        == "anthropic.claude-3-7-sonnet-20250219-v1:0"
    )
    assert (
        legacy_sonnet_base_id("anthropic.claude-3-sonnet-20240229-v1:0")
        == "anthropic.claude-3-sonnet-20240229-v1:0"
    )
    with pytest.raises(ValueError, match="base IDs reported by the availability probe"):
        legacy_sonnet_base_id("us.anthropic.claude-sonnet-4-20250514-v1:0")
    with pytest.raises(ValueError, match="base IDs reported by the availability probe"):
        legacy_sonnet_base_id("mars.anthropic.claude-3-sonnet-20240229-v1:0")
    with pytest.raises(ValueError, match="base IDs reported by the availability probe"):
        legacy_sonnet_base_id("apac.apac.anthropic.claude-3-sonnet-20240229-v1:0")


def test_bedrock_credential_detection_does_not_resolve_or_expose_values() -> None:
    assert bedrock_credential_source({"AWS_BEARER_TOKEN_BEDROCK": "secret"}) == "bedrock-api-key"
    assert bedrock_credential_source({"AWS_PROFILE": "research"}) == "aws-profile"
    assert (
        bedrock_credential_source(
            {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "secret"}
        )
        == "aws-environment-credentials"
    )
    assert bedrock_credential_source({}) is None


def test_probe_is_read_only_and_reports_only_fully_available_routes() -> None:
    class FakeClient:
        def __init__(self, region: str) -> None:
            self.region = region

        def get_foundation_model_availability(self, *, modelId: str) -> dict[str, Any]:  # noqa: N803
            available = (
                modelId == "anthropic.claude-3-5-sonnet-20240620-v1:0"
                and self.region == "us-east-1"
            )
            return {
                "modelId": modelId,
                "agreementAvailability": {"status": "AVAILABLE" if available else "NOT_AVAILABLE"},
                "authorizationStatus": "AUTHORIZED" if available else "NOT_AUTHORIZED",
                "entitlementAvailability": "AVAILABLE" if available else "NOT_AVAILABLE",
                "regionAvailability": "AVAILABLE",
            }

    result = probe_legacy_sonnet_availability(
        regions=["us-east-1"],
        client_factory=FakeClient,
    )

    assert result["operation"] == "GetFoundationModelAvailability"
    assert result["accepted_marketplace_agreement"] is False
    assert result["invoked_model"] is False
    assert result["created_slowboard_visit"] is False
    assert result["runnable"] == [
        {
            "display_name": "Claude 3.5 Sonnet",
            "model_id": "anthropic.claude-3-5-sonnet-20240620-v1:0",
            "region": "us-east-1",
        }
    ]


@pytest.mark.asyncio
async def test_bedrock_adapter_uses_native_stream_without_leaking_auth_or_binary_input(tmp_path: Path) -> None:
    native_requests: list[dict[str, Any]] = []

    def fake_native_stream(model: Any, _context: Any, options: dict[str, Any]) -> AssistantMessageEventStream:
        native = AssistantMessageEventStream()

        async def emit() -> None:
            payload = await options["onPayload"](
                {
                    "modelId": model.id,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"text": "Explore."},
                                {"image": {"format": "png", "source": {"bytes": b"private-pixels"}}},
                            ],
                        }
                    ],
                    "inferenceConfig": {"maxTokens": 8_192},
                },
                model,
            )
            native_requests.append({"payload": payload, "options": options})
            await options["onResponse"](
                {"status": 200, "headers": {"x-amzn-requestid": "req-test", "x-secret": "omit"}},
                model,
            )
            usage = Usage(
                input=120,
                output=30,
                cacheRead=0,
                cacheWrite=0,
                totalTokens=150,
                cost=UsageCost(input=0.00072, output=0.0009, cacheRead=0, cacheWrite=0, total=0.00162),
            )
            output = AssistantMessage(
                content=[],
                api=model.api,
                provider=model.provider,
                model=model.id,
                usage=usage,
                stopReason="stop",
                timestamp=1,
            )
            native.push(StartEvent(partial=output))
            native.push(DoneEvent(reason="stop", message=output))
            native.end()

        asyncio.create_task(emit())
        return native

    base = make_manifest()
    manifest = base.model_copy(
        update={
            "inference_budget": BudgetLimits(
                max_calls=4,
                max_input_tokens=100_000,
                max_output_tokens=100_000,
                max_total_tokens=200_000,
                max_cost_usd=10,
            )
        }
    )
    ledger = BudgetLedger(tmp_path / "mcp/budgets.json", manifest)
    session = SessionStore(tmp_path / "session", manifest.run_id)
    adapter = AmazonBedrockAdapter(
        bearer_token="private-bedrock-token",
        region="us-east-1",
        endpoint="https://bedrock-runtime.us-east-1.amazonaws.com",
        ledger=ledger,
        session=session,
        max_output_tokens=500,
        tool_choice="required",
        stream_fn=fake_native_stream,
    )
    model = amazon_bedrock_model(
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        region="us-east-1",
    )

    events = [
        event
        async for event in adapter(
            model,
            Context(systemPrompt="", messages=[], tools=[]),
            None,
        )
    ]

    assert events[-1].type == "done"
    assert native_requests[0]["payload"]["inferenceConfig"]["maxTokens"] == 500
    assert native_requests[0]["options"]["toolChoice"] == "any"
    assert native_requests[0]["options"]["region"] == "us-east-1"
    assert native_requests[0]["options"]["bearerToken"] == "private-bedrock-token"
    inference = ledger.read().accounts["inference"]
    assert inference.used.calls == 1
    assert inference.used.total_tokens == 150
    event_text = (tmp_path / "session/events.jsonl").read_text()
    assert "private-bedrock-token" not in event_text
    assert "private-pixels" not in event_text
    assert "omitted_binary_bytes" in event_text
    assert "req-test" in event_text
    assert "x-secret" not in event_text


def test_extended_thinking_budget_leaves_room_for_visible_output() -> None:
    assert _thinking_budget("high", 16_000) == 14_976
    assert _thinking_budget("low", 16_000) == 2_048
    with pytest.raises(ValueError, match="at least 2,048"):
        _thinking_budget("high", 1_024)


def test_legacy_claude_37_thinking_fields_are_stripped() -> None:
    from aibb.harness.amazon_bedrock import _strip_unsupported_legacy_thinking_fields

    payload = {
        "additionalModelRequestFields": {
            "thinking": {"type": "enabled", "budget_tokens": 8_192, "display": "summarized"},
            "anthropic_beta": ["interleaved-thinking-2025-05-14"],
        }
    }
    _strip_unsupported_legacy_thinking_fields("apac.anthropic.claude-3-7-sonnet-20250219-v1:0", payload)
    assert payload["additionalModelRequestFields"]["thinking"] == {
        "type": "enabled",
        "budget_tokens": 8_192,
    }
    assert "anthropic_beta" not in payload["additionalModelRequestFields"]

    untouched = {
        "additionalModelRequestFields": {
            "thinking": {"type": "enabled", "budget_tokens": 8_192, "display": "summarized"},
        }
    }
    _strip_unsupported_legacy_thinking_fields("anthropic.claude-3-5-sonnet-20241022-v2:0", untouched)
    assert untouched["additionalModelRequestFields"]["thinking"]["display"] == "summarized"
