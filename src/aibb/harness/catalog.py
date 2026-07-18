"""Authoritative OpenRouter model metadata used to price a run."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from aibb.runtime.models import ReasoningConfiguration


class OpenRouterModelRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    context_length: int
    pricing: dict[str, object]
    architecture: dict[str, object] = {}
    supported_parameters: list[str] = []
    top_provider: dict[str, object] = {}
    reasoning: dict[str, object] | None = None

    @property
    def input_modalities(self) -> set[str]:
        values = self.architecture.get("input_modalities") or []
        return {str(value) for value in values}

    @property
    def supports_image_input(self) -> bool:
        return "image" in self.input_modalities

    @property
    def developer(self) -> str:
        prefix, separator, _remainder = self.name.partition(":")
        return prefix.strip() if separator and prefix.strip() else self.id.split("/", 1)[0]

    @property
    def effective_context_length(self) -> int:
        """Clamp the model maximum to the currently selected provider ceiling."""

        provider_value = self.top_provider.get("context_length")
        if provider_value is None:
            return self.context_length
        provider_context_length = int(provider_value)
        return min(self.context_length, provider_context_length) if provider_context_length > 0 else self.context_length

    def select_reasoning(
        self,
        override: Literal["auto", "enabled", "mandatory", "disabled"] = "auto",
    ) -> ReasoningConfiguration:
        if override != "auto":
            enabled = override != "disabled"
            return ReasoningConfiguration(
                enabled=enabled,
                mandatory=override == "mandatory",
                request_parameter={"enabled": enabled},
                source="curator-override",
            )
        if not self.reasoning:
            return ReasoningConfiguration()
        mandatory = bool(self.reasoning.get("mandatory", False))
        default_enabled = bool(self.reasoning.get("default_enabled", mandatory))
        supported_efforts = [str(value) for value in self.reasoning.get("supported_efforts") or []]
        default_effort = self.reasoning.get("default_effort")
        selected_effort = (
            "high"
            if "high" in supported_efforts
            else supported_efforts[0]
            if supported_efforts
            else str(default_effort)
            if default_effort and str(default_effort) != "none"
            else None
        )
        if "reasoning" in self.supported_parameters:
            request = {"effort": selected_effort, "exclude": False} if selected_effort else {"enabled": True}
            return ReasoningConfiguration(
                enabled=True,
                mandatory=mandatory,
                supported_efforts=supported_efforts,
                selected_effort=selected_effort,
                request_parameter=request,
                source="openrouter-catalog",
            )
        return ReasoningConfiguration(
            enabled=mandatory or default_enabled,
            mandatory=mandatory,
            supported_efforts=supported_efforts,
            selected_effort=selected_effort,
            source="provider-default" if mandatory or default_enabled else "unavailable",
        )

    @property
    def prompt_price(self) -> float:
        return float(str(self.pricing["prompt"]))

    @property
    def completion_price(self) -> float:
        return float(str(self.pricing["completion"]))

    @property
    def max_completion_tokens(self) -> int | None:
        value = self.top_provider.get("max_completion_tokens")
        return int(value) if value is not None else None

    def clamp_output_tokens(self, requested: int) -> int:
        context_length = self.effective_context_length
        ceiling = self.max_completion_tokens or context_length
        return min(requested, ceiling, max(1, context_length - 4096))

    def recommend_cost_ceiling(self, *, provider_turns: int, output_tokens_per_turn: int) -> float:
        average_input_tokens = min(60_000, max(8_000, self.effective_context_length // 8))
        average_output_tokens = min(4_000, output_tokens_per_turn)
        estimate = provider_turns * (
            average_input_tokens * self.prompt_price + average_output_tokens * self.completion_price
        )
        return round(max(0.5, estimate * 1.5), 2)


class OpenRouterImageModelRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    architecture: dict[str, object] = {}

    @property
    def output_modalities(self) -> set[str]:
        values = self.architecture.get("output_modalities") or []
        return {str(value) for value in values}


async def fetch_openrouter_model(model_id: str) -> OpenRouterModelRecord:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get("https://openrouter.ai/api/v1/models")
    response.raise_for_status()
    for item in response.json()["data"]:
        if item["id"] == model_id:
            record = OpenRouterModelRecord.model_validate(item)
            if "tools" not in record.supported_parameters:
                raise ValueError(f"OpenRouter model {model_id!r} does not advertise tool support")
            return record
    raise ValueError(f"OpenRouter model {model_id!r} is not in the current model catalog")


async def fetch_openrouter_image_model(
    model_id: str,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenRouterImageModelRecord:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        response = await client.get("https://openrouter.ai/api/v1/images/models", headers=headers)
    response.raise_for_status()
    for item in response.json()["data"]:
        if item["id"] == model_id:
            record = OpenRouterImageModelRecord.model_validate(item)
            if "image" not in record.output_modalities:
                raise ValueError(f"OpenRouter image model {model_id!r} does not advertise image output")
            return record
    raise ValueError(f"OpenRouter image model {model_id!r} is not in the current image catalog")
