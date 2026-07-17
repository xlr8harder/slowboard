"""Append-only private session events and atomic engine checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aibb.harness.engine import EngineSnapshot

Visibility = Literal["model", "operator", "private_provider", "public_candidate"]


class SessionStoreError(ValueError):
    """Raised when durable session state is missing, corrupt, or inconsistent."""


class SessionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    sequence: int = Field(ge=1)
    timestamp: str
    type: str
    visibility: Visibility
    payload: dict[str, Any]
    previous_hash: str | None
    event_hash: str


class SessionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    event_sequence: int = Field(ge=0)
    event_hash: str | None
    engine: EngineSnapshot


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _event_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _payload_without_hash(event: SessionEvent) -> dict[str, Any]:
    return event.model_dump(mode="json", exclude={"event_hash"})


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SessionStore:
    """One run's durable private state."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root.resolve()
        self.run_id = run_id
        self.events_path = self.root / "events.jsonl"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def read_events(self) -> list[SessionEvent]:
        if not self.events_path.exists():
            return []
        events: list[SessionEvent] = []
        previous_hash: str | None = None
        for line_number, line in enumerate(self.events_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = SessionEvent.model_validate_json(line)
            except Exception as error:
                raise SessionStoreError(f"Invalid session event at line {line_number}: {error}") from error
            if event.run_id != self.run_id:
                raise SessionStoreError(f"Event {line_number} belongs to run {event.run_id}, expected {self.run_id}")
            expected_sequence = len(events) + 1
            if event.sequence != expected_sequence:
                raise SessionStoreError(
                    f"Event {line_number} has sequence {event.sequence}, expected {expected_sequence}"
                )
            if event.previous_hash != previous_hash:
                raise SessionStoreError(f"Event {line_number} has an invalid previous-event hash")
            expected_hash = _event_hash(_payload_without_hash(event))
            if event.event_hash != expected_hash:
                raise SessionStoreError(f"Event {line_number} has an invalid content hash")
            events.append(event)
            previous_hash = event.event_hash
        return events

    def append(self, event_type: str, payload: dict[str, Any], visibility: Visibility) -> SessionEvent:
        events = self.read_events()
        event_payload = {
            "schema_version": 1,
            "run_id": self.run_id,
            "sequence": len(events) + 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "type": event_type,
            "visibility": visibility,
            "payload": payload,
            "previous_hash": events[-1].event_hash if events else None,
        }
        event = SessionEvent(**event_payload, event_hash=_event_hash(event_payload))
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical_json(event.model_dump(mode="json")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def write_checkpoint(self, engine: EngineSnapshot) -> SessionCheckpoint:
        events = self.read_events()
        checkpoint = SessionCheckpoint(
            run_id=self.run_id,
            event_sequence=len(events),
            event_hash=events[-1].event_hash if events else None,
            engine=engine,
        )
        payload = _canonical_json(checkpoint.model_dump(mode="json")) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=".checkpoint-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, self.checkpoint_path)
        _fsync_directory(self.root)
        return checkpoint

    def read_checkpoint(self) -> SessionCheckpoint:
        try:
            checkpoint = SessionCheckpoint.model_validate_json(self.checkpoint_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise SessionStoreError(f"Missing session checkpoint: {self.checkpoint_path}") from error
        except Exception as error:
            raise SessionStoreError(f"Invalid session checkpoint: {error}") from error
        if checkpoint.run_id != self.run_id:
            raise SessionStoreError(f"Checkpoint belongs to run {checkpoint.run_id}, expected {self.run_id}")
        events = self.read_events()
        current_hash = events[-1].event_hash if events else None
        if checkpoint.event_sequence != len(events) or checkpoint.event_hash != current_hash:
            raise SessionStoreError("Checkpoint does not describe the current durable event boundary")
        return checkpoint
