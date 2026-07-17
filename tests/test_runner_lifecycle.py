from __future__ import annotations

from pathlib import Path

from test_archive_build import _write_archive
from test_budget import make_manifest

from aibb.harness.runner import _check_collision, _turn_boundary_outcome


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
