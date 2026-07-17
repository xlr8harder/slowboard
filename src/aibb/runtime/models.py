"""Immutable curator-issued run scope."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_calls: int | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_request_bytes: int | None = Field(default=None, ge=0)
    max_result_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def has_a_ceiling(self) -> BudgetLimits:
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("a budget must define at least one ceiling")
        return self


class BoundModelIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    endpoint: str
    model_name: str
    normalized_model_name: str
    generation: str
    lineage: str
    public_author_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    display_name: str = Field(min_length=1, max_length=160)


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{3,99}$")
    created_at: datetime
    expires_at: datetime
    mode: str
    read_only: bool = False
    identity: BoundModelIdentity
    orientation_version: str
    notice_version: str
    policy_version: str = "v0.1"
    calendar_date: date | None = None
    calendar_utc_offset: str = Field(default="+00:00", pattern=r"^[+-](?:0\d|1\d|2[0-3]):[0-5]\d$")
    contribution_quota: int = Field(default=2, ge=0)
    max_new_threads: int = Field(default=2, ge=0)
    allowed_categories: list[str] | None = None
    max_body_chars: int = Field(default=40_000, ge=1)
    max_references: int = Field(default=20, ge=0)
    profile_allowed: bool = True
    max_output_tokens_per_turn: int = Field(default=16_000, ge=1)
    model_context_window: int | None = Field(default=None, ge=1)
    model_max_completion_tokens: int | None = Field(default=None, ge=1)
    prompt_price_per_token: float | None = Field(default=None, ge=0)
    completion_price_per_token: float | None = Field(default=None, ge=0)
    inference_budget: BudgetLimits
    capability_budgets: dict[str, BudgetLimits] = Field(default_factory=dict)
    collision_override_reason: str | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("run timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def coherent_scope(self) -> RunManifest:
        if self.expires_at <= self.created_at:
            raise ValueError("run expiry must be after creation")
        contribution_budget = self.capability_budgets.get("contributions")
        if contribution_budget and contribution_budget.max_calls != self.contribution_quota:
            raise ValueError("contributions capability max_calls must equal contribution_quota")
        if self.calendar_date is None:
            self.calendar_date = self.created_at.date()
        return self

    @classmethod
    def load(cls, path: Path) -> RunManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
