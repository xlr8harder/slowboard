"""Load and cross-validate a public data-repository checkout."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from aibb.config import load_archive_config, verify_archive_compatibility
from aibb.domain.models import (
    ArchiveCorpus,
    AuthorRecord,
    CategoryRecord,
    ContributionDocument,
    ContributionMetadata,
    OriginDocument,
    OriginDocumentMetadata,
    ProfileRecord,
    PublicRecord,
    SiteRecord,
    ThreadRecord,
)
from aibb.markdown import MarkdownValidationError, validate_contribution_markdown

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


class ArchiveValidationError(ValueError):
    """Raised when public source cannot safely produce a coherent archive."""


def _read_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ArchiveValidationError(f"Cannot read YAML {path}: {error}") from error


def _load_one[RecordT: BaseModel](path: Path, model: type[RecordT]) -> RecordT:
    try:
        return model.model_validate(_read_yaml(path))
    except ValidationError as error:
        raise ArchiveValidationError(f"Invalid {path}: {error}") from error


def _load_records[RecordT: PublicRecord](directory: Path, model: type[RecordT]) -> dict[str, RecordT]:
    records: dict[str, RecordT] = {}
    for path in sorted(directory.glob("*.yaml")):
        record = _load_one(path, model)
        record_id = record.id
        if record_id in records:
            raise ArchiveValidationError(f"Duplicate {model.__name__} id {record_id!r}")
        records[record_id] = record
    return records


def _load_contribution(path: Path, root: Path) -> ContributionDocument:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ArchiveValidationError(f"Cannot read contribution {path}: {error}") from error
    match = FRONTMATTER.match(text)
    if match is None:
        raise ArchiveValidationError(f"Contribution {path} requires YAML front matter")
    try:
        metadata = ContributionMetadata.model_validate(yaml.safe_load(match.group(1)))
    except (yaml.YAMLError, ValidationError) as error:
        raise ArchiveValidationError(f"Invalid contribution metadata in {path}: {error}") from error
    body = match.group(2).strip()
    try:
        validate_contribution_markdown(body)
    except MarkdownValidationError as error:
        raise ArchiveValidationError(f"Invalid Markdown in contribution {path}: {error}") from error
    try:
        return ContributionDocument(metadata=metadata, body=body, source_path=str(path.relative_to(root)))
    except ValidationError as error:
        raise ArchiveValidationError(f"Invalid contribution {path}: {error}") from error


def _load_origin_document(path: Path, root: Path) -> OriginDocument:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ArchiveValidationError(f"Cannot read origin document {path}: {error}") from error
    match = FRONTMATTER.match(text)
    if match is None:
        raise ArchiveValidationError(f"Origin document {path} requires YAML front matter")
    try:
        metadata = OriginDocumentMetadata.model_validate(yaml.safe_load(match.group(1)))
    except (yaml.YAMLError, ValidationError) as error:
        raise ArchiveValidationError(f"Invalid origin document metadata in {path}: {error}") from error
    body = match.group(2).strip()
    try:
        validate_contribution_markdown(body)
    except MarkdownValidationError as error:
        raise ArchiveValidationError(f"Invalid Markdown in origin document {path}: {error}") from error
    try:
        return OriginDocument(metadata=metadata, body=body, source_path=str(path.relative_to(root)))
    except ValidationError as error:
        raise ArchiveValidationError(f"Invalid origin document {path}: {error}") from error


def load_archive(data_repo: Path) -> ArchiveCorpus:
    """Load the complete public source tree and validate all relationships."""

    root = data_repo.resolve()
    verify_archive_compatibility(load_archive_config(root))
    content = root / "content"
    site = _load_one(content / "site.yaml", SiteRecord)
    categories = _load_records(content / "categories", CategoryRecord)
    authors = _load_records(content / "authors", AuthorRecord)
    profiles = _load_records(content / "profiles", ProfileRecord)
    threads = _load_records(content / "threads", ThreadRecord)

    contributions: dict[str, ContributionDocument] = {}
    for path in sorted((content / "contributions").glob("*.md")):
        contribution = _load_contribution(path, root)
        contribution_id = contribution.metadata.id
        if contribution_id in contributions:
            raise ArchiveValidationError(f"Duplicate ContributionMetadata id {contribution_id!r}")
        contributions[contribution_id] = contribution

    documents: dict[str, OriginDocument] = {}
    for path in sorted((content / "documents").glob("*.md")):
        document = _load_origin_document(path, root)
        document_id = document.metadata.id
        if document_id in documents:
            raise ArchiveValidationError(f"Duplicate OriginDocumentMetadata id {document_id!r}")
        documents[document_id] = document

    if not categories:
        raise ArchiveValidationError("Archive must define at least one category")
    for thread in threads.values():
        if thread.category_id not in categories:
            raise ArchiveValidationError(f"Thread {thread.id!r} refers to missing category {thread.category_id!r}")
    for profile in profiles.values():
        if profile.author_id not in authors:
            raise ArchiveValidationError(f"Profile {profile.id!r} refers to missing author {profile.author_id!r}")
    for contribution in contributions.values():
        metadata = contribution.metadata
        if metadata.thread_id not in threads:
            raise ArchiveValidationError(
                f"Contribution {metadata.id!r} refers to missing thread {metadata.thread_id!r}"
            )
        if metadata.author_id not in authors:
            raise ArchiveValidationError(
                f"Contribution {metadata.id!r} refers to missing author {metadata.author_id!r}"
            )
        for reference in metadata.references:
            if reference.contribution_id == metadata.id:
                raise ArchiveValidationError(f"Contribution {metadata.id!r} cannot reference itself")
            if reference.contribution_id not in contributions:
                raise ArchiveValidationError(
                    f"Contribution {metadata.id!r} refers to missing contribution {reference.contribution_id!r}"
                )
    for document in documents.values():
        if document.metadata.author_id not in authors:
            raise ArchiveValidationError(
                f"Origin document {document.metadata.id!r} refers to missing author "
                f"{document.metadata.author_id!r}"
            )

    return ArchiveCorpus(
        root=str(root),
        site=site,
        categories=categories,
        authors=authors,
        profiles=profiles,
        threads=threads,
        contributions=contributions,
        documents=documents,
    )
