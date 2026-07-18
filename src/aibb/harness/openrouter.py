"""Lossless OpenRouter Chat Completions adapter for the controlled Harn loop."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Literal

import httpx
from harn_ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    ModelCost,
    StartEvent,
    TextContent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCall,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
    UsageCost,
)
from harn_ai.utils.event_stream import AssistantMessageEventStream

from aibb.runtime import BudgetLedger
from aibb.runtime.budget import Usage as LedgerUsage
from aibb.sessions.store import SessionStore

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
REASONING_DETAILS_SIGNATURE_PREFIX = "openrouter-reasoning-details:"


def _encode_reasoning_details(value: list[dict[str, Any]]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return REASONING_DETAILS_SIGNATURE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_reasoning_details(value: str | None) -> list[dict[str, Any]] | None:
    if not value or not value.startswith(REASONING_DETAILS_SIGNATURE_PREFIX):
        return None
    encoded = value.removeprefix(REASONING_DETAILS_SIGNATURE_PREFIX)
    decoded = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    return decoded if isinstance(decoded, list) else None


def openrouter_model(
    model_id: str,
    *,
    context_window: int,
    max_tokens: int,
    prompt_price_per_token: float,
    completion_price_per_token: float,
    image_input_supported: bool = False,
    reasoning_enabled: bool = False,
) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        api="aibb-openrouter-chat-completions",
        provider="openrouter",
        baseUrl=OPENROUTER_ENDPOINT,
        reasoning=reasoning_enabled,
        input=["text", "image"] if image_input_supported else ["text"],
        cost=ModelCost(
            input=prompt_price_per_token * 1_000_000,
            output=completion_price_per_token * 1_000_000,
            cacheRead=0,
            cacheWrite=0,
        ),
        contextWindow=context_window,
        maxTokens=max_tokens,
    )


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(block.text for block in value if getattr(block, "type", None) == "text")


def _image_content(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return []
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{block.mimeType};base64,{block.data}"},
        }
        for block in value
        if getattr(block, "type", None) == "image"
    ]


def _messages(context: Context, *, image_input_supported: bool = False) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if context.systemPrompt:
        messages.append({"role": "system", "content": context.systemPrompt})
    pending_images: list[dict[str, Any]] = []
    for index, message in enumerate(context.messages):
        if message.role == "user":
            messages.append({"role": "user", "content": _text_content(message.content)})
        elif message.role == "assistant":
            tool_calls = []
            text = []
            reasoning = []
            reasoning_details: list[dict[str, Any]] = []
            for block in message.content:
                if block.type == "text":
                    text.append(block.text)
                elif block.type == "thinking":
                    reasoning.append(block.thinking)
                    details = _decode_reasoning_details(block.thinkingSignature)
                    if details:
                        reasoning_details.extend(details)
                elif block.type == "toolCall":
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(block.arguments, ensure_ascii=False, separators=(",", ":")),
                            },
                        }
                    )
            payload: dict[str, Any] = {"role": "assistant", "content": "\n".join(text) or None}
            if tool_calls:
                payload["tool_calls"] = tool_calls
            if reasoning_details:
                payload["reasoning_details"] = reasoning_details
            elif reasoning:
                payload["reasoning"] = "\n".join(reasoning)
            messages.append(payload)
        elif message.role == "toolResult":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message.toolCallId,
                    "content": _text_content(message.content),
                }
            )
            if image_input_supported:
                pending_images.extend(_image_content(message.content))
                next_role = (
                    getattr(context.messages[index + 1], "role", None) if index + 1 < len(context.messages) else None
                )
                if next_role != "toolResult" and pending_images:
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "The controlled harness is presenting image output from the preceding "
                                        "tool result(s) for visual inspection."
                                    ),
                                },
                                *pending_images,
                            ],
                        }
                    )
                    pending_images = []
    return messages


def _empty_usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


class OpenRouterAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        ledger: BudgetLedger,
        session: SessionStore,
        max_output_tokens: int,
        prompt_price_per_token: float,
        completion_price_per_token: float,
        app_url: str,
        reasoning_parameter: dict[str, object] | None = None,
        tool_choice: Literal["auto", "required"] = "auto",
        timeout_seconds: float = 180,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.ledger = ledger
        self.session = session
        self.max_output_tokens = max_output_tokens
        self.prompt_price_per_token = prompt_price_per_token
        self.completion_price_per_token = completion_price_per_token
        self.app_url = app_url
        self.reasoning_parameter = dict(reasoning_parameter) if reasoning_parameter else None
        self.tool_choice = tool_choice
        self.timeout_seconds = timeout_seconds
        self.transport = transport
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
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            stopReason="stop",
            timestamp=int(time.time() * 1000),
        )
        reservation_key = self._next_key()
        payload = {
            "model": model.id,
            "messages": _messages(context, image_input_supported="image" in model.input),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in (context.tools or [])
            ],
            "tool_choice": self.tool_choice,
            "max_tokens": min(self.max_output_tokens, model.maxTokens),
            "stream": False,
        }
        if not payload["tools"]:
            payload.pop("tools")
            payload.pop("tool_choice")
        if self.reasoning_parameter:
            payload["reasoning"] = self.reasoning_parameter
        estimated_input = max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)
        available_output = model.contextWindow - estimated_input
        if available_output < 1:
            message = f"Estimated input ({estimated_input}) exceeds the model context window ({model.contextWindow})"
            self.session.append(
                "provider_error",
                {"reservation_key": reservation_key, "type": "ContextWindowError", "message": message},
                "private_provider",
            )
            output.stopReason = "error"
            output.errorMessage = message
            stream.push(ErrorEvent(reason="error", error=output))
            stream.end()
            return
        effective_output = min(int(payload["max_tokens"]), available_output)
        payload["max_tokens"] = effective_output
        self.last_payload = payload
        estimated_input = max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)
        reserved_cost = (
            estimated_input * self.prompt_price_per_token + effective_output * self.completion_price_per_token
        )
        requested = LedgerUsage(
            calls=1,
            input_tokens=estimated_input,
            output_tokens=effective_output,
            total_tokens=estimated_input + effective_output,
            cost_usd=reserved_cost,
            request_bytes=len(json.dumps(payload, ensure_ascii=False).encode()),
        )
        try:
            self.ledger.reserve("inference", reservation_key, requested)
            self.session.append(
                "provider_request",
                {"reservation_key": reservation_key, "endpoint": OPENROUTER_ENDPOINT, "payload": payload},
                "private_provider",
            )
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    OPENROUTER_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": self.app_url,
                        "X-Title": "Slowboard controlled harness",
                    },
                    json=payload,
                )
            response.raise_for_status()
            raw = response.json()
            self.last_response = raw
            self.session.append(
                "provider_response",
                {
                    "reservation_key": reservation_key,
                    "http_status": response.status_code,
                    "headers": {
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() in {"x-request-id", "openrouter-processing-time", "content-type"}
                    },
                    "response": raw,
                },
                "private_provider",
            )
            usage_payload = raw.get("usage") or {}
            input_tokens = int(usage_payload.get("prompt_tokens") or 0)
            output_tokens = int(usage_payload.get("completion_tokens") or 0)
            total_tokens = int(usage_payload.get("total_tokens") or input_tokens + output_tokens)
            actual_cost = float(
                usage_payload.get("cost")
                or input_tokens * self.prompt_price_per_token + output_tokens * self.completion_price_per_token
            )
            self.ledger.reconcile(
                "inference",
                reservation_key,
                LedgerUsage(
                    calls=1,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    cost_usd=actual_cost,
                    request_bytes=requested.request_bytes,
                    result_bytes=len(response.content),
                ),
            )
            output.usage = Usage(
                input=input_tokens,
                output=output_tokens,
                cacheRead=int(usage_payload.get("prompt_tokens_details", {}).get("cached_tokens") or 0),
                cacheWrite=0,
                totalTokens=total_tokens,
                cost=UsageCost(
                    input=input_tokens * self.prompt_price_per_token,
                    output=output_tokens * self.completion_price_per_token,
                    cacheRead=0,
                    cacheWrite=0,
                    total=actual_cost,
                ),
            )
            output.responseId = raw.get("id")
            output.responseModel = raw.get("model")
            choice = raw["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason")
            raw_tool_calls = message.get("tool_calls") or []
            output.stopReason = (
                "toolUse"
                if raw_tool_calls or finish_reason in {"tool_calls", "function_call"}
                else ("length" if finish_reason == "length" else "stop")
            )
            stream.push(StartEvent(partial=output))
            reasoning = message.get("reasoning")
            reasoning_details = message.get("reasoning_details")
            valid_reasoning_details = (
                [item for item in reasoning_details if isinstance(item, dict)]
                if isinstance(reasoning_details, list)
                else []
            )
            detail_text = "\n".join(
                str(item.get("summary") or item.get("text") or "")
                for item in valid_reasoning_details
                if item.get("summary") or item.get("text")
            )
            thinking_text = reasoning if isinstance(reasoning, str) else detail_text
            if thinking_text or valid_reasoning_details:
                block = ThinkingContent(
                    thinking=thinking_text or "[Provider reasoning state retained without visible text]",
                    thinkingSignature=(
                        _encode_reasoning_details(valid_reasoning_details)
                        if valid_reasoning_details
                        else "openrouter-reasoning"
                    ),
                    redacted=not bool(thinking_text),
                )
                output.content.append(block)
                index = len(output.content) - 1
                stream.push(ThinkingStartEvent(contentIndex=index, partial=output))
                stream.push(ThinkingEndEvent(contentIndex=index, content=block.thinking, partial=output))
            content = message.get("content")
            if isinstance(content, str) and content:
                block = TextContent(text=content)
                output.content.append(block)
                index = len(output.content) - 1
                stream.push(TextStartEvent(contentIndex=index, partial=output))
                stream.push(TextEndEvent(contentIndex=index, content=content, partial=output))
            for raw_call in raw_tool_calls:
                function = raw_call["function"]
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError as error:
                    raise RuntimeError(f"Provider returned invalid tool arguments: {error}") from error
                block = ToolCall(id=raw_call["id"], name=function["name"], arguments=arguments)
                output.content.append(block)
                index = len(output.content) - 1
                stream.push(ToolCallStartEvent(contentIndex=index, partial=output))
                stream.push(ToolCallEndEvent(contentIndex=index, toolCall=block, partial=output))
            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            account = self.ledger.read().accounts["inference"]
            if reservation_key in account.reservations:
                self.ledger.reconcile(
                    "inference",
                    reservation_key,
                    LedgerUsage(calls=1, request_bytes=requested.request_bytes),
                )
            self.session.append(
                "provider_error",
                {"reservation_key": reservation_key, "type": type(error).__name__, "message": str(error)},
                "private_provider",
            )
            output.stopReason = "error"
            output.errorMessage = str(error)
            stream.push(ErrorEvent(reason="error", error=output))
        finally:
            stream.end()
