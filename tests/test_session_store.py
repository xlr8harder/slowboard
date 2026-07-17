import json
from pathlib import Path

import pytest
from harn_ai.providers.faux import register_faux_provider

from aibb.harness import AibbHarnessEngine
from aibb.sessions import SessionStore, SessionStoreError


def snapshot_for_test() -> object:
    registration = register_faux_provider({"api": "session-store-faux"})
    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt="exact prompt",
            tools=[],
            stream_fn=lambda *_args: None,
            provider_state={"opaque": "retained"},
        )
        return engine.snapshot()
    finally:
        registration.unregister()


def test_event_chain_and_atomic_checkpoint_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "run", "run-1")
    first = store.append("run_created", {"model": "fixture"}, "operator")
    second = store.append("context_envelope", {"hash": "abc"}, "model")
    checkpoint = store.write_checkpoint(snapshot_for_test())

    assert first.sequence == 1
    assert second.previous_hash == first.event_hash
    assert checkpoint.event_hash == second.event_hash
    assert store.read_checkpoint().engine.provider_state == {"opaque": "retained"}


def test_event_tampering_is_detected(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "run", "run-1")
    store.append("run_created", {"model": "fixture"}, "operator")
    payload = json.loads(store.events_path.read_text(encoding="utf-8"))
    payload["payload"]["model"] = "tampered"
    store.events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(SessionStoreError, match="invalid content hash"):
        store.read_events()
