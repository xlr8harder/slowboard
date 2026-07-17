"""Crash-resilient reserve/dispatch/reconcile accounting for run capabilities."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from aibb.runtime.models import BudgetLimits, RunManifest


class BudgetExceededError(ValueError):
    """Raised before dispatch when a reservation would exceed its ceiling."""


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    request_bytes: int = Field(default=0, ge=0)
    result_bytes: int = Field(default=0, ge=0)

    def plus(self, other: Usage) -> Usage:
        return Usage(**{name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields})


class Reservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    requested: Usage


class BudgetAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limits: BudgetLimits
    used: Usage = Field(default_factory=Usage)
    reservations: dict[str, Reservation] = Field(default_factory=dict)
    settled: dict[str, Usage] = Field(default_factory=dict)


class BudgetState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    accounts: dict[str, BudgetAccount]


_LIMIT_FIELDS = {
    "calls": "max_calls",
    "input_tokens": "max_input_tokens",
    "output_tokens": "max_output_tokens",
    "total_tokens": "max_total_tokens",
    "cost_usd": "max_cost_usd",
    "request_bytes": "max_request_bytes",
    "result_bytes": "max_result_bytes",
}


class BudgetLedger:
    def __init__(self, path: Path, manifest: RunManifest) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest
        if not self.path.exists():
            accounts = {"inference": BudgetAccount(limits=manifest.inference_budget)}
            capabilities = dict(manifest.capability_budgets)
            capabilities.setdefault("contributions", BudgetLimits(max_calls=manifest.contribution_quota))
            accounts.update({name: BudgetAccount(limits=limits) for name, limits in capabilities.items()})
            self._write(BudgetState(run_id=manifest.run_id, accounts=accounts))
        state = self.read()
        if state.run_id != manifest.run_id:
            raise ValueError(f"Budget ledger belongs to run {state.run_id}, expected {manifest.run_id}")

    def read(self) -> BudgetState:
        return BudgetState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def _write(self, state: BudgetState) -> None:
        payload = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, prefix=".budgets-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def _account(self, state: BudgetState, name: str) -> BudgetAccount:
        try:
            return state.accounts[name]
        except KeyError as error:
            raise BudgetExceededError(f"Capability {name!r} is not enabled for this run") from error

    @staticmethod
    def _reserved(account: BudgetAccount) -> Usage:
        total = Usage()
        for reservation in account.reservations.values():
            total = total.plus(reservation.requested)
        return total

    @staticmethod
    def _check(name: str, account: BudgetAccount, requested: Usage) -> None:
        projected = account.used.plus(BudgetLedger._reserved(account)).plus(requested)
        for usage_field, limit_field in _LIMIT_FIELDS.items():
            limit = getattr(account.limits, limit_field)
            if limit is not None and getattr(projected, usage_field) > limit:
                raise BudgetExceededError(
                    f"{name} budget would exceed {limit_field}: {getattr(projected, usage_field)} > {limit}"
                )

    def reserve(self, name: str, idempotency_key: str, requested: Usage) -> Reservation:
        state = self.read()
        account = self._account(state, name)
        if idempotency_key in account.settled:
            return Reservation(idempotency_key=idempotency_key, requested=account.settled[idempotency_key])
        if idempotency_key in account.reservations:
            existing = account.reservations[idempotency_key]
            if existing.requested != requested:
                raise ValueError("idempotency key was already used with different requested usage")
            return existing
        self._check(name, account, requested)
        reservation = Reservation(idempotency_key=idempotency_key, requested=requested)
        account.reservations[idempotency_key] = reservation
        self._write(state)
        return reservation

    def reconcile(self, name: str, idempotency_key: str, actual: Usage | None = None) -> Usage:
        state = self.read()
        account = self._account(state, name)
        if idempotency_key in account.settled:
            return account.settled[idempotency_key]
        try:
            reservation = account.reservations.pop(idempotency_key)
        except KeyError as error:
            raise ValueError(f"No outstanding {name!r} reservation for {idempotency_key!r}") from error
        charged = actual or reservation.requested
        account.used = account.used.plus(charged)
        account.settled[idempotency_key] = charged
        self._write(state)
        return charged

    def remaining(self) -> dict[str, dict[str, int | float | None]]:
        result: dict[str, dict[str, int | float | None]] = {}
        for name, account in self.read().accounts.items():
            committed = account.used.plus(self._reserved(account))
            result[name] = {
                limit_field: None
                if getattr(account.limits, limit_field) is None
                else max(0, getattr(account.limits, limit_field) - getattr(committed, usage_field))
                for usage_field, limit_field in _LIMIT_FIELDS.items()
            }
        return result
