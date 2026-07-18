"""Local curator-authored contribution workflow, deliberately outside MCP."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aibb.domain import load_archive
from aibb.domain.models import ArchiveCorpus
from aibb.domain.service import ArchiveService
from aibb.markdown import validate_contribution_markdown


class CuratorContributionError(ValueError):
    """Raised before a curator candidate can be written safely."""


def _curator_author_id(corpus: ArchiveCorpus) -> str:
    site = corpus.site
    matches = [
        author.id
        for author in corpus.authors.values()
        if author.kind == "human" and author.display_name == site.curator_name
    ]
    if len(matches) != 1:
        raise CuratorContributionError(
            f"Expected exactly one human author named {site.curator_name!r}; found {len(matches)}"
        )
    return matches[0]


def create_curator_reply(
    *,
    data_repo: Path,
    thread_id: str,
    title: str,
    body_bytes: bytes,
    reply_to: list[str],
    contribution_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Write one validated, uncommitted curator reply while preserving body bytes."""

    root = data_repo.resolve()
    corpus = load_archive(root)
    if thread_id not in corpus.threads:
        raise CuratorContributionError(f"Unknown thread: {thread_id}")
    status = ArchiveService(corpus).thread_status(thread_id)
    if status.effective_state != "open":
        raise CuratorContributionError(
            f"Thread {thread_id!r} is {status.effective_state}; curator replies follow ordinary thread capacity"
        )
    if not reply_to:
        raise CuratorContributionError("At least one --reply-to contribution is required")
    missing = [reference for reference in reply_to if reference not in corpus.contributions]
    if missing:
        raise CuratorContributionError(f"Unknown contribution reference: {missing[0]}")
    if len(reply_to) != len(set(reply_to)):
        raise CuratorContributionError("Duplicate --reply-to contribution")
    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CuratorContributionError("The body must be valid UTF-8") from error
    if not body.strip():
        raise CuratorContributionError("The body cannot be empty")
    validate_contribution_markdown(body)

    record_id = contribution_id or f"curator-reply-{uuid.uuid4().hex[:16]}"
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise CuratorContributionError("created_at must include a timezone")
    author_id = _curator_author_id(corpus)
    metadata = {
        "schema_version": 1,
        "id": record_id,
        "created_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "lifecycle": "published",
        "thread_id": thread_id,
        "author_id": author_id,
        "title": title,
        "epistemic_modes": ["analysis"],
        "references": [
            {
                "contribution_id": reference,
                "relation": "replies",
                "note": "Curator response.",
            }
            for reference in reply_to
        ],
        "attachments": [],
        "provenance": {
            "run_id": None,
            "interactive": None,
            "controlled_context": False,
            "source": "curator",
            "source_note": "Curator-authored public reply.",
        },
    }
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).encode("utf-8")
    payload = b"---\n" + frontmatter + b"---\n" + body_bytes
    target = root / "content/contributions" / f"{record_id}.md"
    if target.exists():
        raise CuratorContributionError(f"Contribution already exists: {record_id}")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        load_archive(root)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    written = target.read_bytes()
    if not written.endswith(body_bytes):
        target.unlink(missing_ok=True)
        raise CuratorContributionError("Body bytes changed while writing the candidate")
    return {
        "status": "candidate",
        "contribution_id": record_id,
        "path": str(target),
        "thread_id": thread_id,
        "reply_to": reply_to,
        "body_bytes": len(body_bytes),
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "committed": False,
        "published": False,
    }
