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

    @property
    def prompt_price(self) -> float:
        return float(str(self.pricing["prompt"]))

    @property
    def completion_price(self) -> float:
        return float(str(self.pricing["completion"]))


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
