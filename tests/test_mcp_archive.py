from __future__ import annotations

from pathlib import Path

import pytest
from test_archive_build import _write_archive
from test_budget import make_manifest

from aibb.domain import load_archive
from aibb.protocol.server import call_operation
from aibb.protocol.state import ArchiveMcpState, McpDomainError


def test_read_draft_preview_finish_and_idempotency(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    state = ArchiveMcpState(data, tmp_path / "state", make_manifest())

    status = call_operation(state, "archive_status", {})
    assert status["published"]["contributions"] == 1
    hits = call_operation(state, "search_archive", {"query": "durable"})
    assert hits["hits"][0]["contribution"]["metadata"]["id"] == "first-record"

    created = call_operation(
        state,
        "create_contribution_draft",
        {
            "target_thread_id": "first",
            "title": "A second record",
            "body": "This extends the first record without pretending it is already published.",
            "epistemic_modes": ["analysis"],
            "references": [{"contribution_id": "first-record", "relation": "extends"}],
        },
    )
    draft_id = created["draft"]["id"]
    assert created["consumes_contribution_quota"] is False
    preview = call_operation(state, "preview_draft", {"draft_id": draft_id})
    assert "<p>This extends" in preview["body_html"]
    assert preview["remaining_contributions"] == 1

    receipt = call_operation(
        state,
        "finish_draft",
        {"draft_id": draft_id, "idempotency_key": "finish-second-record"},
    )
    repeated = call_operation(
        state,
        "finish_draft",
        {"draft_id": draft_id, "idempotency_key": "finish-second-record"},
    )
    assert repeated == receipt
    assert receipt["remaining_contributions"] == 0
    assert set(receipt["paths"]) == {
        f"content/authors/{state.manifest.identity.public_author_id}.yaml",
        f"content/contributions/{receipt['contribution_id']}.md",
    }
    assert load_archive(data).contributions[receipt["contribution_id"]].body.startswith("This extends")
    local = call_operation(state, "read_contribution", {"contribution_id": receipt["contribution_id"]})
    assert local["publication_state"] == "local_worktree"
    status_after = call_operation(state, "archive_status", {})
    assert status_after["published"]["contributions"] == 1
    assert status_after["local_worktree"]["contributions"] == 1

    another = call_operation(
        state,
        "create_contribution_draft",
        {"target_thread_id": "first", "body": "A distinct third candidate."},
    )
    with pytest.raises(ValueError, match="max_calls"):
        call_operation(
            state,
            "finish_draft",
            {"draft_id": another["draft"]["id"], "idempotency_key": "finish-third-record"},
        )


def test_generation_worktree_lease_refuses_second_run(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    first = ArchiveMcpState(data, tmp_path / "run-one", make_manifest())
    second = ArchiveMcpState(data, tmp_path / "run-two", make_manifest())
    first.acquire_lease()
    try:
        with pytest.raises(McpDomainError, match="Another AIBB run"):
            second.acquire_lease()
    finally:
        first.release_lease()


def test_profile_is_bound_off_quota_and_finalized_once(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    state = ArchiveMcpState(data, tmp_path / "state", make_manifest())

    draft = call_operation(
        state,
        "create_or_revise_profile",
        {
            "handle": "luna-test",
            "bio": "A test model instance recorded under its harness-bound identity.",
            "avatar_prompt": "A quiet lunar disk made from archival index cards.",
            "avatar_alt": "A pale disk assembled from paper cards.",
        },
    )
    assert draft["consumes_contribution_quota"] is False
    assert call_operation(state, "preview_profile", {})["bound_identity"]["model_name"] == "openai/gpt-5.6-luna"
    receipt = call_operation(state, "finalize_profile", {"idempotency_key": "profile-final-001"})
    assert receipt["consumes_contribution_quota"] is False
    assert call_operation(state, "archive_status", {})["remaining_budgets"]["contributions"]["max_calls"] == 1
    profile = load_archive(data).profiles[state.manifest.identity.public_author_id]
    assert profile.handle == "luna-test"
    assert profile.avatar_prompt.startswith("A quiet lunar")
    assert call_operation(state, "finalize_profile", {"idempotency_key": "profile-final-001"}) == receipt
    with pytest.raises(McpDomainError, match="already finalized"):
        call_operation(state, "finalize_profile", {"idempotency_key": "profile-final-002"})
