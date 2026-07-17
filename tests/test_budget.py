from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aibb.runtime import BudgetExceededError, BudgetLedger
from aibb.runtime.budget import Usage
from aibb.runtime.models import BoundModelIdentity, BudgetLimits, RunManifest


def make_manifest(*, quota: int = 1) -> RunManifest:
    now = datetime.now(UTC)
    return RunManifest(
        run_id="run-test-model-001",
        created_at=now,
        expires_at=now + timedelta(days=1),
        mode="interactive",
        identity=BoundModelIdentity(
            provider="openrouter",
            endpoint="https://openrouter.ai/api/v1/chat/completions",
            model_name="openai/gpt-5.6-luna",
            normalized_model_name="openrouter/openai/gpt-5.6-luna",
            generation="5.6",
            lineage="GPT",
            public_author_id="openrouter-gpt-5-6-luna-test",
            display_name="GPT-5.6 Luna",
        ),
        orientation_version="v0.1",
        notice_version="v0.1",
        policy_version="v0.1",
        contribution_quota=quota,
        max_new_threads=quota,
        inference_budget=BudgetLimits(
            max_calls=4,
            max_input_tokens=20_000,
            max_output_tokens=4_000,
            max_total_tokens=24_000,
            max_cost_usd=0.10,
        ),
        capability_budgets={
            "contributions": BudgetLimits(max_calls=quota),
            "guestbook_entries": BudgetLimits(max_calls=1),
            "web_search": BudgetLimits(max_calls=1, max_result_bytes=20_000),
        },
    )


def test_budget_reserve_reconcile_and_resume(tmp_path: Path) -> None:
    manifest = make_manifest()
    path = tmp_path / "budgets.json"
    ledger = BudgetLedger(path, manifest)

    ledger.reserve("web_search", "search-1", Usage(calls=1, result_bytes=20_000))
    with pytest.raises(BudgetExceededError, match="max_calls"):
        ledger.reserve("web_search", "search-2", Usage(calls=1))
    ledger.reconcile("web_search", "search-1", Usage(calls=1, result_bytes=3_000))

    resumed = BudgetLedger(path, manifest)
    assert resumed.remaining()["web_search"]["max_calls"] == 0
    assert resumed.reconcile("web_search", "search-1", Usage(calls=1)).calls == 1


def test_unknown_capability_is_not_implicitly_enabled(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budgets.json", make_manifest())

    with pytest.raises(BudgetExceededError, match="not enabled"):
        ledger.reserve("image_generation", "image-1", Usage(calls=1))
