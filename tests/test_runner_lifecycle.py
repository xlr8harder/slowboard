from __future__ import annotations

from pathlib import Path

from test_archive_build import _write_archive
from test_budget import make_manifest

from aibb.harness.engine import EngineSnapshot
from aibb.harness.runner import (
    CURRENT_ORIENTATION_VERSION,
    _check_collision,
    _remove_failed_assistant_placeholder,
    _turn_boundary_outcome,
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
