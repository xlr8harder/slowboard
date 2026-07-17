"""Authoritative OpenRouter model metadata used to price a run."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict


class OpenRouterModelRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    context_length: int
    pricing: dict[str, object]
    supported_parameters: list[str] = []
    top_provider: dict[str, object] = {}
    reasoning: dict[str, object] | None = None

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
        ceiling = self.max_completion_tokens or self.context_length
        return min(requested, ceiling, max(1, self.context_length - 4096))

    def recommend_cost_ceiling(self, *, provider_turns: int, output_tokens_per_turn: int) -> float:
        average_input_tokens = min(60_000, max(8_000, self.context_length // 8))
        average_output_tokens = min(4_000, output_tokens_per_turn)
        estimate = provider_turns * (
            average_input_tokens * self.prompt_price + average_output_tokens * self.completion_price
        )
        return round(max(0.5, estimate * 1.5), 2)


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
