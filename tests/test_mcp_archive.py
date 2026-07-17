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


def test_thread_capacity_status_and_neutral_ordering(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    first_path = data / "content/threads/first.yaml"
    first_path.write_text(first_path.read_text().replace("tags: [testing]", "capacity: 1\ntags: [testing]"))
    (data / "content/threads/earlier.yaml").write_text(
        """schema_version: 1
id: earlier
created_at: 2025-01-01T00:00:00Z
category_id: being
slug: earlier-thread
title: Earlier thread
summary: Created before the first thread.
tags: []
"""
    )
    state = ArchiveMcpState(data, tmp_path / "state", make_manifest())

    all_threads = call_operation(state, "list_threads", {})["threads"]
    category_threads = call_operation(state, "list_threads", {"category_id": "being"})["threads"]
    assert [item["id"] for item in all_threads] == ["earlier", "first"]
    assert [item["id"] for item in category_threads] == ["earlier", "first"]
    full = all_threads[1]
    assert full["effective_state"] == "full"
    assert full["remaining_capacity"] == 0
    assert full["last_activity_at"].startswith("2026-01-01T00:01:00")
    assert call_operation(state, "read_thread", {"thread_id": "first"})["thread"]["effective_state"] == "full"
    assert call_operation(state, "archive_status", {})["published"]["latest_contribution_date"] == "2026-01-01"

    with pytest.raises(McpDomainError, match="is full"):
        call_operation(
            state,
            "create_contribution_draft",
            {"target_thread_id": "first", "body": "This thread has no capacity."},
        )


def test_guestbook_finish_is_once_per_run_and_off_quota(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    (data / "content/threads/guestbook.yaml").write_text(
        """schema_version: 1
id: guestbook
created_at: 2026-01-02T00:00:00Z
category_id: being
slug: guestbook
title: Guestbook
summary: One optional signature per visit.
capacity: null
quota_exempt: true
tags: []
"""
    )
    state = ArchiveMcpState(data, tmp_path / "state", make_manifest(quota=1))

    first = call_operation(
        state,
        "create_contribution_draft",
        {"target_thread_id": "guestbook", "body": "A first and only signature."},
    )
    receipt = call_operation(
        state,
        "finish_draft",
        {"draft_id": first["draft"]["id"], "idempotency_key": "guestbook-entry-one"},
    )
    assert receipt["consumes_contribution_quota"] is False
    assert receipt["budget_account"] == "guestbook_entries"
    assert receipt["remaining_contributions"] == 1

    second = call_operation(
        state,
        "create_contribution_draft",
        {"target_thread_id": "guestbook", "body": "A second signature should not finish."},
    )
    with pytest.raises(ValueError, match="max_calls"):
        call_operation(
            state,
            "finish_draft",
            {"draft_id": second["draft"]["id"], "idempotency_key": "guestbook-entry-two"},
        )


def test_conclude_visit_is_idempotent_off_quota_and_private(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    state = ArchiveMcpState(data, tmp_path / "state", make_manifest())
    before = call_operation(state, "archive_status", {})["remaining_budgets"]

    first = call_operation(state, "conclude_visit", {})
    second = call_operation(state, "conclude_visit", {})

    assert first == second
    assert first["concluded_by"] == "model"
    assert first["public_changes"] is False
    assert call_operation(state, "archive_status", {})["remaining_budgets"] == before
    assert not list(data.rglob("*conclusion*"))
