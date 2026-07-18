"""Budgeted Slowboard boundary around Harn's native Anthropic adapter."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, Literal

from harn_ai.models import get_model
from harn_ai.providers.anthropic import stream_anthropic
from harn_ai.types import AssistantMessage, AssistantMessageEvent, Context, ErrorEvent, Model, Usage, UsageCost
from harn_ai.utils.event_stream import AssistantMessageEventStream

from aibb.runtime import BudgetLedger
from aibb.runtime.budget import Usage as LedgerUsage
from aibb.sessions.store import SessionStore

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"


def anthropic_model(model_id: str) -> Model:
    """Return a detached, pinned Harn catalog record for an Anthropic model."""

    model = get_model("anthropic", model_id)
    if model is None:
        raise ValueError(f"Unknown Anthropic model ID: {model_id}")
    if model.api != "anthropic-messages":
        raise ValueError(f"Anthropic model {model_id!r} does not use the Messages API")
    return model.model_copy(deep=True)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


class AnthropicAdapter:
    """Add durable request/usage logging and budget checks to Harn's adapter."""

    def __init__(
        self,
        *,
        api_key: str,
        ledger: BudgetLedger,
        session: SessionStore,
        max_output_tokens: int,
        tool_choice: Literal["auto", "required"] = "auto",
        timeout_seconds: float = 180,
        stream_fn: Callable[..., AssistantMessageEventStream] = stream_anthropic,
    ) -> None:
        self._api_key = api_key
        self.ledger = ledger
        self.session = session
        self.max_output_tokens = max_output_tokens
        self.tool_choice = tool_choice
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
            requested_max = min(int(normalized.get("max_tokens") or model.maxTokens), self.max_output_tokens)
            normalized["max_tokens"] = requested_max
            estimated_input = max(1, len(_json_bytes(normalized)) // 4)
            available_output = model.contextWindow - estimated_input
            if available_output < 1:
                raise RuntimeError(
                    f"Estimated input ({estimated_input}) exceeds the model context window ({model.contextWindow})"
                )
            normalized["max_tokens"] = min(requested_max, available_output)
            payload_bytes = _json_bytes(normalized)
            estimated_input = max(1, len(payload_bytes) // 4)
            effective_output = int(normalized["max_tokens"])
            requested = LedgerUsage(
                calls=1,
                input_tokens=estimated_input,
                output_tokens=effective_output,
                total_tokens=estimated_input + effective_output,
                cost_usd=(
                    estimated_input * model.cost.input / 1_000_000
                    + effective_output * model.cost.output / 1_000_000
                ),
                request_bytes=len(payload_bytes),
            )
            self.ledger.reserve("inference", reservation_key, requested)
            self.last_payload = normalized
            self.session.append(
                "provider_request",
                {"reservation_key": reservation_key, "endpoint": ANTHROPIC_ENDPOINT, "payload": normalized},
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
                        if name.casefold() in {"content-type", "request-id", "x-request-id"}
                    },
                }
            )

        try:
            native = self.stream_fn(
                model,
                context,
                {
                    "apiKey": self._api_key,
                    "maxTokens": min(self.max_output_tokens, model.maxTokens),
                    "maxRetries": 0,
                    "timeoutMs": int(self.timeout_seconds * 1000),
                    "thinkingEnabled": False,
                    "interleavedThinking": False,
                    "cacheRetention": "none",
                    "toolChoice": "any" if self.tool_choice == "required" else "auto",
                    "onPayload": on_payload,
                    "onResponse": on_response,
                },
            )
            async for event in native:
                if event.type in {"done", "error"}:
                    terminal = event
                else:
                    stream.push(event)
            if terminal is None:
                raise RuntimeError("Anthropic adapter ended without a terminal event")

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
                    result_bytes=len(_json_bytes(output.model_dump(mode="json", by_alias=True, exclude_none=True))),
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
                        "type": "AnthropicProviderError",
                        "message": output.errorMessage or "Anthropic provider response failed",
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
