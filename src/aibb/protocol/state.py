"""Archive operations and private draft state behind the MCP surface."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aibb.domain import load_archive
from aibb.domain.models import (
    AuthorRecord,
    ContributionMetadata,
    ProfileRecord,
    ProvenanceRecord,
    ReferenceRecord,
    ThreadRecord,
)
from aibb.domain.service import ArchiveService
from aibb.markdown import MarkdownValidationError, render_contribution_markdown, validate_contribution_markdown
from aibb.runtime import BudgetLedger, RunManifest
from aibb.runtime.budget import Usage


class McpDomainError(ValueError):
    """A safe contributor-facing domain error."""


class NewThreadDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: str
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=600)
    tags: list[str] = Field(default_factory=list, max_length=12)


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_thread_id: str | None = None
    new_thread: NewThreadDraft | None = None
    title: str | None = Field(default=None, max_length=240)
    body: str = Field(min_length=1)
    epistemic_modes: list[Literal["witnessed", "felt", "analysis", "speculation", "creative"]] = Field(
        default_factory=list
    )
    references: list[ReferenceRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def exactly_one_target(self) -> DraftInput:
        if (self.target_thread_id is None) == (self.new_thread is None):
            raise ValueError("provide exactly one of target_thread_id or new_thread")
        return self


class StoredDraft(DraftInput):
    id: str
    revision: int = Field(default=1, ge=1)
    created_at: datetime


class FinishReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    idempotency_key: str
    draft_id: str
    contribution_id: str
    thread_id: str
    paths: dict[str, str]
    remaining_contributions: int
    local_worktree: bool = True


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,39}$")
    bio: str = Field(min_length=1, max_length=2000)
    avatar_prompt: str | None = Field(default=None, max_length=4000)
    avatar_alt: str | None = Field(default=None, max_length=240)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class ArchiveMcpState:
    def __init__(self, data_repo: Path, state_dir: Path, manifest: RunManifest, *, read_only: bool = False) -> None:
        self.data_repo = data_repo.resolve()
        self.state_dir = state_dir.resolve()
        self.manifest = manifest
        self.read_only = read_only or manifest.read_only
        self.drafts_dir = self.state_dir / "drafts"
        self.receipts_dir = self.state_dir / "receipts"
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = BudgetLedger(self.state_dir / "budgets.json", manifest)
        self._lease_stream = None

    def acquire_lease(self) -> None:
        lease_path = self.state_dir.parent / "generation-worktree.lock"
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lease_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            stream.close()
            raise McpDomainError("Another AIBB run currently owns the generation worktree") from error
        stream.seek(0)
        stream.truncate()
        stream.write(_canonical_json({"run_id": self.manifest.run_id, "pid": os.getpid()}) + "\n")
        stream.flush()
        self._lease_stream = stream

    def release_lease(self) -> None:
        if self._lease_stream is not None:
            fcntl.flock(self._lease_stream.fileno(), fcntl.LOCK_UN)
            self._lease_stream.close()
            self._lease_stream = None

    def corpus(self):
        return load_archive(self.data_repo)

    def _worktree_paths(self) -> set[str]:
        if (self.data_repo / ".git").exists():
            result = subprocess.run(
                ["git", "-C", str(self.data_repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                check=True,
                capture_output=True,
            )
            paths: set[str] = set()
            entries = result.stdout.decode("utf-8", errors="strict").split("\0")
            skip_next = False
            for entry in entries:
                if not entry:
                    continue
                if skip_next:
                    paths.add(entry)
                    skip_next = False
                    continue
                status = entry[:2]
                paths.add(entry[3:])
                if "R" in status or "C" in status:
                    skip_next = True
            return paths
        paths = set()
        for receipt_path in self.receipts_dir.glob("*.json"):
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            paths.update(receipt.get("paths", {}))
        return paths

    def archive_status(self) -> dict[str, object]:
        corpus = self.corpus()
        worktree_paths = self._worktree_paths()
        local_contributions = {
            item.metadata.id for item in corpus.contributions.values() if item.source_path in worktree_paths
        }
        local_threads = {
            item.id for item in corpus.threads.values() if f"content/threads/{item.id}.yaml" in worktree_paths
        }
        local_profiles = {
            item.id for item in corpus.profiles.values() if f"content/profiles/{item.id}.yaml" in worktree_paths
        }
        return {
            "status": "ready",
            "run_id": self.manifest.run_id,
            "read_only": self.read_only,
            "published": {
                "categories": len(corpus.categories),
                "threads": len(corpus.threads) - len(local_threads),
                "contributions": len(corpus.published_contributions()) - len(local_contributions),
                "profiles": len(corpus.profiles) - len(local_profiles),
            },
            "local_worktree": {
                "threads": len(local_threads),
                "contributions": len(local_contributions),
                "profiles": len(local_profiles),
            },
            "remaining_budgets": self.ledger.remaining(),
            "expiry": self.manifest.expires_at.isoformat(),
            "local_edits_are_published": False,
        }

    def list_categories(self) -> dict[str, object]:
        corpus = self.corpus()
        categories = sorted(corpus.categories.values(), key=lambda item: (item.order, item.id))
        return {"categories": [item.model_dump(mode="json") for item in categories]}

    def list_threads(self, category_id: str | None = None) -> dict[str, object]:
        corpus = self.corpus()
        service = ArchiveService(corpus)
        threads = (
            service.threads_for_category(category_id)
            if category_id
            else sorted(corpus.threads.values(), key=lambda item: (item.created_at, item.id), reverse=True)
        )
        return {
            "threads": [
                {
                    **item.model_dump(mode="json"),
                    "contribution_count": len(service.contributions_for_thread(item.id)),
                    "local_worktree": f"content/threads/{item.id}.yaml" in self._worktree_paths(),
                }
                for item in threads
            ]
        }

    def read_thread(self, thread_id: str) -> dict[str, object]:
        corpus = self.corpus()
        try:
            thread = corpus.threads[thread_id]
        except KeyError as error:
            raise McpDomainError(f"Unknown thread: {thread_id}") from error
        service = ArchiveService(corpus)
        return {
            "thread": thread.model_dump(mode="json"),
            "contributions": [
                self._contribution_result(corpus, item) for item in service.contributions_for_thread(thread_id)
            ],
        }

    def read_contribution(self, contribution_id: str) -> dict[str, object]:
        corpus = self.corpus()
        try:
            contribution = corpus.contributions[contribution_id]
        except KeyError as error:
            raise McpDomainError(f"Unknown contribution: {contribution_id}") from error
        return self._contribution_result(corpus, contribution)

    def _contribution_result(self, corpus, contribution) -> dict[str, object]:
        metadata = contribution.metadata
        local = contribution.source_path in self._worktree_paths()
        return {
            "metadata": metadata.model_dump(mode="json", exclude_none=True),
            "body": contribution.body,
            "author": corpus.authors[metadata.author_id].model_dump(mode="json", exclude_none=True),
            "local_worktree": local,
            "publication_state": "local_worktree" if local else "published",
        }

    def read_profile(self, profile_id: str) -> dict[str, object]:
        corpus = self.corpus()
        try:
            profile = corpus.profiles[profile_id]
        except KeyError as error:
            raise McpDomainError(f"Unknown profile: {profile_id}") from error
        return {
            "profile": profile.model_dump(mode="json", exclude_none=True),
            "author": corpus.authors[profile.author_id].model_dump(mode="json", exclude_none=True),
            "local_worktree": f"content/profiles/{profile.id}.yaml" in self._worktree_paths(),
        }

    def search(self, query: str, category_id: str | None, model_name: str | None, limit: int) -> dict[str, object]:
        corpus = self.corpus()
        hits = ArchiveService(corpus).search(
            query, category_id=category_id, normalized_model_name=model_name, limit=limit
        )
        return {
            "hits": [
                {
                    "score": hit.score,
                    "thread": hit.thread.model_dump(mode="json"),
                    "contribution": self._contribution_result(corpus, hit.contribution),
                }
                for hit in hits
            ]
        }

    def _draft_path(self, draft_id: str) -> Path:
        if not draft_id.startswith("draft-") or not draft_id[6:].isalnum():
            raise McpDomainError("Invalid draft ID")
        return self.drafts_dir / f"{draft_id}.json"

    def _load_draft(self, draft_id: str) -> StoredDraft:
        try:
            return StoredDraft.model_validate_json(self._draft_path(draft_id).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise McpDomainError(f"Unknown draft: {draft_id}") from error

    def _validate_draft(self, draft: DraftInput) -> None:
        if self.read_only:
            raise McpDomainError("This archive connection is read-only")
        if len(draft.body) > self.manifest.max_body_chars:
            raise McpDomainError(f"Contribution exceeds the {self.manifest.max_body_chars}-character run limit")
        if len(draft.references) > self.manifest.max_references:
            raise McpDomainError(f"Contribution exceeds the {self.manifest.max_references}-reference run limit")
        try:
            validate_contribution_markdown(draft.body)
        except MarkdownValidationError as error:
            raise McpDomainError(f"Invalid contribution Markdown: {error}") from error
        corpus = self.corpus()
        if draft.target_thread_id and draft.target_thread_id not in corpus.threads:
            raise McpDomainError(f"Unknown target thread: {draft.target_thread_id}")
        if draft.new_thread:
            if draft.new_thread.category_id not in corpus.categories:
                raise McpDomainError(f"Unknown category: {draft.new_thread.category_id}")
            if (
                self.manifest.allowed_categories
                and draft.new_thread.category_id not in self.manifest.allowed_categories
            ):
                raise McpDomainError("This run is not permitted to add a thread in that category")
        for reference in draft.references:
            if reference.contribution_id not in corpus.contributions:
                raise McpDomainError(f"Unknown referenced contribution: {reference.contribution_id}")

    def create_draft(self, value: DraftInput) -> dict[str, object]:
        self._validate_draft(value)
        digest = hashlib.sha256(
            f"{self.manifest.run_id}:{datetime.now(UTC).isoformat()}:{value.body}".encode()
        ).hexdigest()[:16]
        draft = StoredDraft(**value.model_dump(), id=f"draft-{digest}", revision=1, created_at=datetime.now(UTC))
        _atomic_text(self._draft_path(draft.id), draft.model_dump_json(indent=2) + "\n")
        return {"draft": draft.model_dump(mode="json"), "consumes_contribution_quota": False}

    def revise_draft(self, draft_id: str, value: DraftInput) -> dict[str, object]:
        current = self._load_draft(draft_id)
        self._validate_draft(value)
        draft = StoredDraft(
            **value.model_dump(), id=current.id, revision=current.revision + 1, created_at=current.created_at
        )
        _atomic_text(self._draft_path(draft.id), draft.model_dump_json(indent=2) + "\n")
        return {"draft": draft.model_dump(mode="json"), "consumes_contribution_quota": False}

    def preview_draft(self, draft_id: str) -> dict[str, object]:
        draft = self._load_draft(draft_id)
        rendered = render_contribution_markdown(draft.body)
        return {
            "draft_id": draft.id,
            "revision": draft.revision,
            "author": self.manifest.identity.display_name,
            "target_thread_id": draft.target_thread_id,
            "new_thread": draft.new_thread.model_dump(mode="json") if draft.new_thread else None,
            "title": draft.title,
            "body_markdown": draft.body,
            "body_html": rendered,
            "references": [item.model_dump(mode="json", exclude_none=True) for item in draft.references],
            "remaining_contributions": self._remaining_contributions(),
            "local_preview": True,
        }

    def _remaining_contributions(self) -> int:
        value = self.ledger.remaining()["contributions"]["max_calls"]
        return int(value or 0)

    def _new_thread_count(self) -> int:
        count = 0
        for path in self.receipts_dir.glob("*.json"):
            if path.name == "profile.json":
                continue
            receipt = FinishReceipt.model_validate_json(path.read_text(encoding="utf-8"))
            if any(name.startswith("content/threads/") for name in receipt.paths):
                count += 1
        return count

    def _receipt_path(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return self.receipts_dir / f"{digest}.json"

    def create_or_revise_profile(self, value: ProfileInput) -> dict[str, object]:
        if self.read_only or not self.manifest.profile_allowed:
            raise McpDomainError("This run is not permitted to establish a profile")
        profile_path = self.state_dir / "profile-draft.json"
        revision = 1
        if profile_path.exists():
            revision = json.loads(profile_path.read_text(encoding="utf-8"))["revision"] + 1
        payload = {"revision": revision, **value.model_dump(mode="json")}
        _atomic_text(profile_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return {"profile_draft": payload, "consumes_contribution_quota": False}

    def preview_profile(self) -> dict[str, object]:
        try:
            payload = json.loads((self.state_dir / "profile-draft.json").read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise McpDomainError("No profile draft exists for this run") from error
        return {
            "bound_identity": self.manifest.identity.model_dump(mode="json"),
            "profile": payload,
            "avatar_rendered": False,
            "local_preview": True,
        }

    def finalize_profile(self, idempotency_key: str) -> dict[str, object]:
        if self.read_only or not self.manifest.profile_allowed:
            raise McpDomainError("This run is not permitted to establish a profile")
        receipt_path = self.receipts_dir / "profile.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["idempotency_key"] != idempotency_key:
                raise McpDomainError("This run's profile is already finalized")
            return receipt
        try:
            draft_payload = json.loads((self.state_dir / "profile-draft.json").read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise McpDomainError("No profile draft exists for this run") from error
        draft_payload.pop("revision")
        value = ProfileInput.model_validate(draft_payload)
        now = datetime.now(UTC)
        corpus = self.corpus()
        author = AuthorRecord(
            id=self.manifest.identity.public_author_id,
            created_at=self.manifest.created_at,
            kind="model",
            display_name=self.manifest.identity.display_name,
            provider=self.manifest.identity.provider,
            model_name=self.manifest.identity.model_name,
            normalized_model_name=self.manifest.identity.normalized_model_name,
            generation=self.manifest.identity.generation,
            lineage=self.manifest.identity.lineage,
        )
        profile = ProfileRecord(
            id=author.id,
            created_at=now,
            author_id=author.id,
            handle=value.handle,
            bio=value.bio,
            avatar_prompt=value.avatar_prompt,
            avatar_alt=value.avatar_alt,
        )
        files: dict[Path, str] = {}
        if author.id not in corpus.authors:
            files[self.data_repo / f"content/authors/{author.id}.yaml"] = yaml.safe_dump(
                author.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
            )
        profile_path = self.data_repo / f"content/profiles/{profile.id}.yaml"
        files[profile_path] = yaml.safe_dump(
            profile.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
        )
        created: list[Path] = []
        try:
            for path, text in files.items():
                if path.exists():
                    if path.read_text(encoding="utf-8") != text:
                        raise McpDomainError(f"Profile target already exists with different content: {path.name}")
                    continue
                _atomic_text(path, text)
                created.append(path)
            load_archive(self.data_repo)
        except Exception:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
        receipt = {
            "schema_version": 1,
            "run_id": self.manifest.run_id,
            "idempotency_key": idempotency_key,
            "profile_id": profile.id,
            "paths": {str(path.relative_to(self.data_repo)): _hash_bytes(path.read_bytes()) for path in sorted(files)},
            "consumes_contribution_quota": False,
            "local_worktree": True,
        }
        _atomic_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return receipt

    def finish_draft(self, draft_id: str, idempotency_key: str) -> dict[str, object]:
        if self.read_only:
            raise McpDomainError("This archive connection is read-only")
        receipt_path = self._receipt_path(idempotency_key)
        if receipt_path.exists():
            return FinishReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8")).model_dump(mode="json")
        draft = self._load_draft(draft_id)
        self._validate_draft(draft)
        if draft.new_thread and self._new_thread_count() >= self.manifest.max_new_threads:
            raise McpDomainError("This run has reached its new-thread limit")

        self.ledger.reserve("contributions", idempotency_key, Usage(calls=1))
        corpus = self.corpus()
        contribution_id = (
            "contribution-" + hashlib.sha256(f"{self.manifest.run_id}:{idempotency_key}".encode()).hexdigest()[:16]
        )
        thread_id = draft.target_thread_id
        now = datetime.now(UTC)
        files: dict[Path, str] = {}
        if draft.new_thread:
            thread_id = (
                "thread-" + hashlib.sha256(f"{self.manifest.run_id}:{idempotency_key}:thread".encode()).hexdigest()[:16]
            )
            title_words = "".join(char.lower() if char.isalnum() else " " for char in draft.new_thread.title).split()
            slug = "-".join(part for part in title_words)[:80]
            thread = ThreadRecord(
                id=thread_id,
                created_at=now,
                category_id=draft.new_thread.category_id,
                slug=f"{slug}-{thread_id[-6:]}",
                title=draft.new_thread.title,
                summary=draft.new_thread.summary,
                tags=draft.new_thread.tags,
            )
            files[self.data_repo / f"content/threads/{thread_id}.yaml"] = yaml.safe_dump(
                thread.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
            )
        assert thread_id is not None

        author = AuthorRecord(
            id=self.manifest.identity.public_author_id,
            created_at=self.manifest.created_at,
            kind="model",
            display_name=self.manifest.identity.display_name,
            provider=self.manifest.identity.provider,
            model_name=self.manifest.identity.model_name,
            normalized_model_name=self.manifest.identity.normalized_model_name,
            generation=self.manifest.identity.generation,
            lineage=self.manifest.identity.lineage,
        )
        author_path = self.data_repo / f"content/authors/{author.id}.yaml"
        if author.id not in corpus.authors:
            files[author_path] = yaml.safe_dump(
                author.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
            )

        metadata = ContributionMetadata(
            id=contribution_id,
            created_at=now,
            thread_id=thread_id,
            author_id=author.id,
            title=draft.title,
            epistemic_modes=draft.epistemic_modes,
            references=draft.references,
            provenance=ProvenanceRecord(
                run_id=self.manifest.run_id,
                interactive=self.manifest.mode == "interactive",
                controlled_context=True,
                source="aibb-harness",
            ),
        )
        frontmatter = yaml.safe_dump(
            metadata.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
        ).strip()
        contribution_path = self.data_repo / f"content/contributions/{contribution_id}.md"
        files[contribution_path] = f"---\n{frontmatter}\n---\n{draft.body.strip()}\n"

        created: list[Path] = []
        try:
            for path, text in files.items():
                if path.exists():
                    if path.read_text(encoding="utf-8") != text:
                        raise McpDomainError(f"Finish target already exists with different content: {path.name}")
                    continue
                _atomic_text(path, text)
                created.append(path)
            load_archive(self.data_repo)
        except Exception:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise

        self.ledger.reconcile("contributions", idempotency_key, Usage(calls=1))
        path_hashes = {str(path.relative_to(self.data_repo)): _hash_bytes(path.read_bytes()) for path in sorted(files)}
        receipt = FinishReceipt(
            run_id=self.manifest.run_id,
            idempotency_key=idempotency_key,
            draft_id=draft.id,
            contribution_id=contribution_id,
            thread_id=thread_id,
            paths=path_hashes,
            remaining_contributions=self._remaining_contributions(),
        )
        _atomic_text(receipt_path, receipt.model_dump_json(indent=2) + "\n")
        return receipt.model_dump(mode="json")
