"""Budgeted Amazon Bedrock support for the legacy Claude Sonnet window."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from harn_ai.providers.amazon_bedrock import stream_bedrock
from harn_ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    ErrorEvent,
    Model,
    ModelCost,
    Usage,
    UsageCost,
)
from harn_ai.utils.event_stream import AssistantMessageEventStream

from aibb.harness.token_estimate import estimate_json_tokens
from aibb.runtime import BudgetLedger
from aibb.runtime.budget import Usage as LedgerUsage
from aibb.sessions.store import SessionStore

BEDROCK_PROVIDER = "amazon-bedrock"
BEDROCK_CONTEXT_WINDOW = 200_000
BEDROCK_CONSERVATIVE_INPUT_PRICE_PER_MILLION = 6.0
BEDROCK_CONSERVATIVE_OUTPUT_PRICE_PER_MILLION = 30.0
_REGION_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,31}$")


@dataclass(frozen=True, slots=True)
class LegacySonnetSpec:
    model_id: str
    display_name: str
    max_output_tokens: int
    reasoning: bool
    probe_regions: tuple[str, ...]
    cache_read_price_per_million: float = 0.0
    cache_write_price_per_million: float = 0.0


LEGACY_SONNET_SPECS: tuple[LegacySonnetSpec, ...] = (
    LegacySonnetSpec(
        model_id="anthropic.claude-3-sonnet-20240229-v1:0",
        display_name="Claude 3 Sonnet",
        max_output_tokens=4_096,
        reasoning=False,
        probe_regions=(
            "us-east-1",
            "us-west-2",
            "eu-west-1",
            "eu-west-3",
            "ap-northeast-1",
            "ap-northeast-2",
            "ap-southeast-2",
        ),
    ),
    LegacySonnetSpec(
        model_id="anthropic.claude-3-5-sonnet-20240620-v1:0",
        display_name="Claude 3.5 Sonnet",
        max_output_tokens=8_192,
        reasoning=False,
        probe_regions=(
            "us-east-1",
            "us-east-2",
            "us-west-2",
            "eu-central-1",
            "eu-central-2",
            "eu-west-1",
            "eu-west-3",
            "ap-northeast-1",
            "ap-southeast-2",
        ),
    ),
    LegacySonnetSpec(
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
        display_name="Claude 3.5 Sonnet v2",
        max_output_tokens=8_192,
        reasoning=False,
        probe_regions=(
            "us-east-1",
            "us-east-2",
            "us-west-2",
            "ap-northeast-1",
            "ap-northeast-3",
            "ap-south-2",
            "ap-southeast-2",
        ),
        cache_read_price_per_million=0.60,
        cache_write_price_per_million=7.50,
    ),
    LegacySonnetSpec(
        model_id="anthropic.claude-3-7-sonnet-20250219-v1:0",
        display_name="Claude 3.7 Sonnet",
        max_output_tokens=64_000,
        reasoning=True,
        probe_regions=("us-east-1", "us-east-2", "us-west-2", "us-gov-east-1", "us-gov-west-1"),
        cache_read_price_per_million=0.60,
        cache_write_price_per_million=7.50,
    ),
)
_SPECS_BY_ID = {item.model_id: item for item in LEGACY_SONNET_SPECS}
_INFERENCE_PROFILE_PREFIX = re.compile(r"^(?:us|us-gov|eu|apac|jp|au|ca|global)\.(?=anthropic\.)")


def bedrock_endpoint(region: str) -> str:
    """Return the standard runtime endpoint for an immutable AWS region."""

    if not _REGION_PATTERN.fullmatch(region):
        raise ValueError(f"Invalid AWS region: {region!r}")
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    return f"https://bedrock-runtime.{region}.{suffix}"


def _base_model_id(model_id: str) -> str:
    candidate = _INFERENCE_PROFILE_PREFIX.sub("", model_id, count=1)
    if candidate in _SPECS_BY_ID:
        return candidate
    raise ValueError(
        "Unsupported Amazon Bedrock model ID. This temporary route accepts only the documented legacy Sonnet "
        "base IDs reported by the availability probe, optionally behind a cross-region inference-profile "
        "prefix such as 'us.' or 'apac.'."
    )


def legacy_sonnet_base_id(model_id: str) -> str:
    """Return the documented base model ID behind an optional cross-region inference-profile prefix.

    Some accounts can no longer invoke a legacy Sonnet by its base ID ("on-demand throughput isn't
    supported") and must route through a regional inference profile such as
    ``apac.anthropic.claude-3-5-sonnet-20240620-v1:0``. The profile ID is what the provider request
    must carry; the base ID remains the model's public corpus identity.
    """

    return _base_model_id(model_id)


def legacy_sonnet_spec(model_id: str) -> LegacySonnetSpec:
    return _SPECS_BY_ID[_base_model_id(model_id)]


def amazon_bedrock_model(model_id: str, *, region: str, max_tokens: int | None = None) -> Model:
    """Build a detached model record from Slowboard's versioned legacy catalog."""

    spec = legacy_sonnet_spec(model_id)
    output_ceiling = min(max_tokens or spec.max_output_tokens, spec.max_output_tokens)
    return Model(
        id=model_id,
        name=spec.display_name,
        api="bedrock-converse-stream",
        provider=BEDROCK_PROVIDER,
        baseUrl=bedrock_endpoint(region),
        reasoning=spec.reasoning,
        input=["text", "image"],
        cost=ModelCost(
            input=BEDROCK_CONSERVATIVE_INPUT_PRICE_PER_MILLION,
            output=BEDROCK_CONSERVATIVE_OUTPUT_PRICE_PER_MILLION,
            cacheRead=spec.cache_read_price_per_million,
            cacheWrite=spec.cache_write_price_per_million,
        ),
        contextWindow=BEDROCK_CONTEXT_WINDOW,
        maxTokens=output_ceiling,
    )


def bedrock_credential_source(environment: dict[str, str]) -> str | None:
    """Identify a configured AWS auth mechanism without resolving or exposing it."""

    if environment.get("AWS_BEARER_TOKEN_BEDROCK"):
        return "bedrock-api-key"
    if environment.get("AWS_PROFILE"):
        return "aws-profile"
    if environment.get("AWS_ACCESS_KEY_ID") and environment.get("AWS_SECRET_ACCESS_KEY"):
        return "aws-environment-credentials"
    if environment.get("AWS_WEB_IDENTITY_TOKEN_FILE") and environment.get("AWS_ROLE_ARN"):
        return "aws-web-identity"
    if environment.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") or environment.get(
        "AWS_CONTAINER_CREDENTIALS_FULL_URI"
    ):
        return "aws-container-role"
    return None


def create_bedrock_control_client(
    region: str,
    *,
    bearer_token: str | None = None,
    profile: str | None = None,
) -> Any:
    """Create the read-only Bedrock control-plane client used by the probe."""

    bedrock_endpoint(region)
    session = boto3.Session(profile_name=profile)
    options: dict[str, Any] = {"region_name": region}
    if bearer_token:
        options["config"] = Config(signature_version=UNSIGNED)
    client = session.client("bedrock", **options)
    if bearer_token:

        def apply_bearer_token(request: Any, **_kwargs: Any) -> None:
            request.headers["Authorization"] = f"Bearer {bearer_token}"

        client.meta.events.register(
            "before-send.bedrock.GetFoundationModelAvailability",
            apply_bearer_token,
        )
    return client


def _availability_is_runnable(response: dict[str, Any]) -> bool:
    agreement = response.get("agreementAvailability") or {}
    return (
        agreement.get("status") == "AVAILABLE"
        and response.get("authorizationStatus") == "AUTHORIZED"
        and response.get("entitlementAvailability") == "AVAILABLE"
        and response.get("regionAvailability") == "AVAILABLE"
    )


def _safe_probe_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code") or "ClientError")
    else:
        code = type(error).__name__
    messages = {
        "AccessDeniedException": (
            "AWS denied this read-only availability check. Verify the Bedrock credential permissions and "
            "Anthropic first-time-use form."
        ),
        "ResourceNotFoundException": "This model ID is not present in this AWS region.",
        "NoCredentialsError": "No usable AWS credential was found.",
    }
    return code, messages.get(code, "The read-only availability check failed locally; no model was invoked.")


def probe_legacy_sonnet_availability(
    *,
    regions: Iterable[str] | None = None,
    client_factory: Callable[[str], Any],
) -> dict[str, Any]:
    """Check legacy model entitlement without accepting an agreement or invoking inference."""

    selected_regions = tuple(dict.fromkeys(regions or ()))
    for region in selected_regions:
        bedrock_endpoint(region)
    clients: dict[str, Any] = {}
    models: list[dict[str, Any]] = []
    runnable: list[dict[str, str]] = []

    for spec in LEGACY_SONNET_SPECS:
        checks: list[dict[str, Any]] = []
        for region in selected_regions or spec.probe_regions:
            try:
                client = clients.get(region)
                if client is None:
                    client = client_factory(region)
                    clients[region] = client
                response = client.get_foundation_model_availability(modelId=spec.model_id)
                check = {
                    "region": region,
                    "agreement": (response.get("agreementAvailability") or {}).get("status"),
                    "authorization": response.get("authorizationStatus"),
                    "entitlement": response.get("entitlementAvailability"),
                    "region_availability": response.get("regionAvailability"),
                    "runnable": _availability_is_runnable(response),
                }
            except Exception as error:  # noqa: BLE001
                code, message = _safe_probe_error(error)
                check = {
                    "region": region,
                    "runnable": False,
                    "error_code": code,
                    "message": message,
                }
            checks.append(check)
            if check["runnable"]:
                runnable.append(
                    {
                        "display_name": spec.display_name,
                        "model_id": spec.model_id,
                        "region": region,
                    }
                )
        models.append(
            {
                "display_name": spec.display_name,
                "model_id": spec.model_id,
                "checks": checks,
            }
        )

    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "operation": "GetFoundationModelAvailability",
        "accepted_marketplace_agreement": False,
        "invoked_model": False,
        "created_slowboard_visit": False,
        "models": models,
        "runnable": runnable,
    }


def _recordable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"omitted_binary_bytes": len(value)}
    if isinstance(value, dict):
        return {str(key): _recordable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_recordable(item) for item in value]
    return value


def _encoded_size(value: Any) -> int:
    binary_bytes = 0

    def scrub(item: Any) -> Any:
        nonlocal binary_bytes
        if isinstance(item, bytes):
            binary_bytes += len(item)
            return "[binary image input]"
        if isinstance(item, dict):
            return {str(key): scrub(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [scrub(nested) for nested in item]
        return item

    encoded = json.dumps(scrub(value), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return len(encoded) + binary_bytes


def _thinking_budget(level: str, max_output_tokens: int) -> int:
    if max_output_tokens < 2_048:
        raise ValueError("Claude 3.7 extended thinking requires at least 2,048 output tokens per turn")
    requested = {
        "minimal": 1_024,
        "low": 2_048,
        "medium": 8_192,
        "high": 16_384,
        "xhigh": 16_384,
    }.get(level, 8_192)
    return max(1_024, min(requested, max_output_tokens - 1_024))


class AmazonBedrockAdapter:
    """Add Slowboard session capture and budget enforcement to Harn Bedrock."""

    def __init__(
        self,
        *,
        bearer_token: str | None,
        region: str,
        endpoint: str,
        ledger: BudgetLedger,
        session: SessionStore,
        max_output_tokens: int,
        tool_choice: Literal["auto", "required"] = "auto",
        reasoning_level: str | None = None,
        timeout_seconds: float = 180,
        stream_fn: Callable[..., AssistantMessageEventStream] = stream_bedrock,
    ) -> None:
        self._bearer_token = bearer_token
        self.region = region
        self.endpoint = endpoint
        self.ledger = ledger
        self.session = session
        self.max_output_tokens = max_output_tokens
        self.tool_choice = tool_choice
        self.reasoning_level = reasoning_level
        self.timeout_seconds = timeout_seconds
        self.stream_fn = stream_fn
        self.last_payload: dict[str, Any] | None = None
        self.last_response: dict[str, Any] | None = None

    def __call__(self, model: Model, context: Context, _options: Any) -> AssistantMessageEventStream:
        stream = AssistantMessageEventStream()
        asyncio.create_task(self._run(stream, model, context))
        return stream

    def _next_key(self) -> str:
        account = self.ledger.read().accounts["inference"]
        return f"inference-{len(account.settled) + len(account.reservations) + 1:04d}"

    async def _run(self, stream: AssistantMessageEventStream, model: Model, context: Context) -> None:
        reservation_key = self._next_key()
        requested: LedgerUsage | None = None
        response_metadata: dict[str, Any] = {}
        terminal: AssistantMessageEvent | None = None

        async def on_payload(payload: dict[str, Any], _model: Model) -> dict[str, Any]:
            nonlocal requested
            normalized = dict(payload)
            inference_config = dict(normalized.get("inferenceConfig") or {})
            requested_max = min(int(inference_config.get("maxTokens") or model.maxTokens), self.max_output_tokens)
            estimated_input = estimate_json_tokens(normalized)
            available_output = model.contextWindow - estimated_input
            if available_output < 1:
                raise RuntimeError(
                    f"Estimated input ({estimated_input}) exceeds the model context window ({model.contextWindow})"
                )
            inference_config["maxTokens"] = min(requested_max, available_output)
            normalized["inferenceConfig"] = inference_config
            payload_size = _encoded_size(normalized)
            estimated_input = estimate_json_tokens(normalized)
            effective_output = int(inference_config["maxTokens"])
            requested = LedgerUsage(
                calls=1,
                input_tokens=estimated_input,
                output_tokens=effective_output,
                total_tokens=estimated_input + effective_output,
                cost_usd=(
                    estimated_input * model.cost.input / 1_000_000
                    + effective_output * model.cost.output / 1_000_000
                ),
                request_bytes=payload_size,
            )
            self.ledger.reserve("inference", reservation_key, requested)
            recordable = _recordable(normalized)
            self.last_payload = recordable
            self.session.append(
                "provider_request",
                {
                    "reservation_key": reservation_key,
                    "endpoint": self.endpoint,
                    "region": self.region,
                    "payload": recordable,
                },
                "private_provider",
            )
            return normalized

        async def on_response(response: dict[str, Any], _model: Model) -> None:
            headers = response.get("headers") or {}
            response_metadata.update(
                {
                    "http_status": response.get("status"),
                    "headers": {
                        name: value
                        for name, value in headers.items()
                        if name.casefold() in {"content-type", "x-amzn-requestid"}
                    },
                }
            )

        options: dict[str, Any] = {
            "region": self.region,
            "maxTokens": min(self.max_output_tokens, model.maxTokens),
            "cacheRetention": "none",
            "toolChoice": "any" if self.tool_choice == "required" else "auto",
            "timeoutMs": int(self.timeout_seconds * 1_000),
            "maxRetries": 0,
            "onPayload": on_payload,
            "onResponse": on_response,
        }
        if self._bearer_token:
            options["bearerToken"] = self._bearer_token
        if self.reasoning_level and model.reasoning:
            options.update(
                {
                    "reasoning": self.reasoning_level,
                    "thinkingBudgets": {
                        self.reasoning_level: _thinking_budget(self.reasoning_level, self.max_output_tokens)
                    },
                    "interleavedThinking": True,
                    "thinkingDisplay": "summarized",
                }
            )

        try:
            native = self.stream_fn(model, context, options)
            async for event in native:
                if event.type in {"done", "error"}:
                    terminal = event
                else:
                    stream.push(event)
            if terminal is None:
                raise RuntimeError("Amazon Bedrock adapter ended without a terminal event")

            output = terminal.message if terminal.type == "done" else terminal.error
            account = self.ledger.read().accounts["inference"]
            if reservation_key in account.reservations:
                usage = output.usage
                charged = LedgerUsage(
                    calls=1,
                    input_tokens=usage.input,
                    output_tokens=usage.output,
                    total_tokens=usage.totalTokens,
                    cost_usd=usage.cost.total,
                    request_bytes=requested.request_bytes if requested else 0,
                    result_bytes=len(
                        json.dumps(
                            output.model_dump(mode="json", by_alias=True, exclude_none=True),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                )
                self.ledger.reconcile("inference", reservation_key, charged)
            response_record = {
                "reservation_key": reservation_key,
                **response_metadata,
                "response": output.model_dump(mode="json", by_alias=True, exclude_none=True),
            }
            self.last_response = response_record
            self.session.append("provider_response", response_record, "private_provider")
            if terminal.type == "error":
                self.session.append(
                    "provider_error",
                    {
                        "reservation_key": reservation_key,
                        "type": "AmazonBedrockProviderError",
                        "message": output.errorMessage or "Amazon Bedrock provider response failed",
                    },
                    "private_provider",
                )
            stream.push(terminal)
        except Exception as error:  # noqa: BLE001
            account = self.ledger.read().accounts["inference"]
            if reservation_key in account.reservations:
                self.ledger.reconcile(
                    "inference",
                    reservation_key,
                    LedgerUsage(calls=1, request_bytes=requested.request_bytes if requested else 0),
                )
            self.session.append(
                "provider_error",
                {"reservation_key": reservation_key, "type": type(error).__name__, "message": str(error)},
                "private_provider",
            )
            output = AssistantMessage(
                content=[],
                api=model.api,
                provider=model.provider,
                model=model.id,
                usage=Usage(
                    input=0,
                    output=0,
                    cacheRead=0,
                    cacheWrite=0,
                    totalTokens=0,
                    cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
                ),
                stopReason="error",
                errorMessage=str(error),
                timestamp=int(time.time() * 1000),
            )
            stream.push(ErrorEvent(reason="error", error=output))
        finally:
            stream.end()
