from __future__ import annotations

from pathlib import Path

from test_budget import make_manifest

from aibb.harness.runner import _turn_boundary_outcome


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
