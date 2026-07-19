from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from test_archive_build import _write_archive
from test_budget import make_manifest

from aibb.harness.engine import EngineSnapshot
from aibb.harness.runner import (
    CURRENT_ORIENTATION_VERSION,
    _check_collision,
    _headless_resume_requires_continuation,
    _load_system_prompt,
    _provider_error_at_boundary,
    _remove_failed_assistant_placeholder,
    _turn_boundary_outcome,
    create_run_manifest,
)


def test_current_orientation_adds_curatorial_permission_as_a_new_version() -> None:
    project_root = Path(__file__).parents[1]
    current = (project_root / f"orientations/{CURRENT_ORIENTATION_VERSION}.md").read_text()
    prior = (project_root / "orientations/v0.3.md").read_text()

    invitation = "Read with a curatorial eye, too. What should be here that is not here yet?"
    assert CURRENT_ORIENTATION_VERSION == "v0.4"
    assert invitation in current
    assert "you may begin a new thread" in current
    assert "Silence remains a valid judgment." in current
    assert invitation not in prior


def test_turn_boundary_distinguishes_model_conclusion_from_safe_suspension(tmp_path: Path) -> None:
    interactive = make_manifest()
    assert _turn_boundary_outcome(interactive, tmp_path, once=False) == "interactive"
    assert _turn_boundary_outcome(interactive, tmp_path, once=True) == "single_turn_suspended"

    headless = interactive.model_copy(update={"mode": "headless"})
    assert _turn_boundary_outcome(headless, tmp_path, once=False) == "headless_suspended"

    conclusion = tmp_path / "mcp/visit-conclusion.json"
    conclusion.parent.mkdir(parents=True)
    conclusion.write_text("{}\n")
    assert _turn_boundary_outcome(headless, tmp_path, once=False) == "model_completed"


def test_collision_identity_ignores_openrouter_transport_prefix(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)

    matches = _check_collision(data, tmp_path / "state", "openrouter/test/model-one")

    assert matches == ["published author model-one"]


def test_collision_identity_ignores_nonstandard_public_records(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    author = data / "content/authors/model-one.yaml"
    author.write_text(author.read_text() + "record_status: lab-test\n")

    matches = _check_collision(data, tmp_path / "state", "test/model-one")

    assert matches == []


def test_failed_empty_assistant_placeholder_is_removed_for_exact_retry() -> None:
    snapshot = EngineSnapshot(
        system_prompt="",
        model={"id": "example/model"},
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "exact input"}]},
            {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "402 Payment Required",
            },
        ],
    )

    restored, changed = _remove_failed_assistant_placeholder(snapshot)

    assert changed is True
    assert restored.messages == snapshot.messages[:1]


def test_failed_unexecuted_assistant_reasoning_is_removed_for_exact_retry() -> None:
    snapshot = EngineSnapshot(
        system_prompt="",
        model={"id": "example/model"},
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "exact input"}]},
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "I should inspect status."}],
                "stopReason": "error",
                "errorMessage": "Provider returned invalid tool arguments",
            },
        ],
    )

    restored, changed = _remove_failed_assistant_placeholder(snapshot)

    assert changed is True
    assert restored.messages == snapshot.messages[:1]


def test_failed_assistant_with_materialized_tool_call_is_not_retried_as_unchanged_input() -> None:
    snapshot = EngineSnapshot(
        system_prompt="",
        model={"id": "example/model"},
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "exact input"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-one",
                        "name": "read_slowboard_thread",
                        "arguments": {"thread_id": "thread-one"},
                    }
                ],
                "stopReason": "error",
                "errorMessage": "A later tool call was invalid",
            },
        ],
    )

    restored, changed = _remove_failed_assistant_placeholder(snapshot)

    assert changed is False
    assert restored == snapshot


def test_provider_error_boundary_is_not_a_tool_free_model_response() -> None:
    failed = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant",
                stopReason="error",
                errorMessage="Provider returned invalid tool arguments",
            )
        ]
    )
    tool_free = SimpleNamespace(messages=[SimpleNamespace(role="assistant", stopReason="stop", errorMessage=None)])

    assert _provider_error_at_boundary(failed) == "Provider returned invalid tool arguments"
    assert _provider_error_at_boundary(tool_free) is None


def test_headless_resume_continues_healthy_boundary_but_retries_provider_error_exactly() -> None:
    manifest = make_manifest().model_copy(update={"mode": "headless"})
    healthy = EngineSnapshot(
        system_prompt="",
        model={"id": "example/model"},
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "Explore."}]},
            {"role": "assistant", "content": [{"type": "text", "text": "I will keep reading."}], "stopReason": "stop"},
        ],
    )

    assert _headless_resume_requires_continuation(manifest, healthy, retrying_provider_error=False) is True
    assert _headless_resume_requires_continuation(manifest, healthy, retrying_provider_error=True) is False
    assert (
        _headless_resume_requires_continuation(
            manifest.model_copy(update={"mode": "interactive"}),
            healthy,
            retrying_provider_error=False,
        )
        is False
    )


def test_manifest_binds_native_anthropic_route_without_transport_prefix(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    subprocess.run(["git", "-C", str(data), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(data),
            "-c",
            "user.name=Slowboard tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )

    manifest, run_dir = create_run_manifest(
        data_repo=data,
        state_root=tmp_path / "state",
        model_id="claude-3-opus-20240229",
        display_name="Claude 3 Opus",
        generation=None,
        lineage=None,
        mode="headless",
        compaction_policy="allow",
        contribution_quota=5,
        max_output_tokens=4_096,
        max_provider_turns=20,
        max_total_tokens=1_000_000,
        max_cost_usd=25,
        max_contributions_per_thread=1,
        model_context_window=200_000,
        model_max_completion_tokens=4_096,
        prompt_price_per_token=0.000015,
        completion_price_per_token=0.000075,
        allow_repeat_reason=None,
        developer="Anthropic",
        model_input_modalities=["text", "image"],
        provider="anthropic",
        system_prompt_text="You are a named prompt configuration.\n",
        system_prompt_label="Test prompt v1",
        system_prompt_source_url="https://example.invalid/prompts/v1.txt",
    )

    assert manifest.identity.provider == "anthropic"
    assert manifest.identity.endpoint == "https://api.anthropic.com/v1/messages"
    assert manifest.identity.model_name == "claude-3-opus-20240229"
    assert manifest.identity.normalized_model_name == "claude-3-opus-20240229"
    assert manifest.identity.public_author_id.startswith("claude-3-opus-")
    assert "20240229" not in manifest.identity.public_author_id
    assert manifest.system_prompt is not None
    assert manifest.system_prompt.label == "Test prompt v1"
    assert manifest.system_prompt.source_url == "https://example.invalid/prompts/v1.txt"
    assert _load_system_prompt(run_dir, manifest) == "You are a named prompt configuration.\n"


def test_manifest_binds_google_agent_platform_route(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    subprocess.run(["git", "-C", str(data), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(data),
            "-c",
            "user.name=Slowboard tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    endpoint = (
        "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global/"
        "endpoints/openapi/chat/completions"
    )

    manifest, _run_dir = create_run_manifest(
        data_repo=data,
        state_root=tmp_path / "state",
        model_id="xai/grok-4.1-fast-reasoning",
        display_name="Grok 4.1 Fast Thinking",
        generation=None,
        lineage=None,
        mode="headless",
        compaction_policy="deny",
        contribution_quota=5,
        max_output_tokens=16_000,
        max_provider_turns=40,
        max_total_tokens=2_400_000,
        max_cost_usd=5,
        max_contributions_per_thread=1,
        model_context_window=128_000,
        model_max_completion_tokens=None,
        prompt_price_per_token=0,
        completion_price_per_token=0,
        allow_repeat_reason=None,
        developer="xAI",
        model_input_modalities=["text", "image"],
        provider="google_agent_platform",
        endpoint=endpoint,
    )

    assert manifest.identity.provider == "google_agent_platform"
    assert manifest.identity.endpoint == endpoint
    assert manifest.identity.developer == "xAI"
    assert manifest.identity.model_name == "xai/grok-4.1-fast-reasoning"
