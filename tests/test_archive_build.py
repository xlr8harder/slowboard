from __future__ import annotations

import json
from pathlib import Path

import pytest

from aibb.domain import ArchiveValidationError, load_archive
from aibb.site import build_site


def _write_archive(root: Path, *, body: str = "A durable contribution.") -> None:
    (root / "content/categories").mkdir(parents=True)
    (root / "content/authors").mkdir()
    (root / "content/profiles").mkdir()
    (root / "content/threads").mkdir()
    (root / "content/contributions").mkdir()
    (root / "aibb.toml").write_text('schema_version = 1\n[builder]\nrequirement = "aibb==0.1.0"\n')
    (root / "content/site.yaml").write_text(
        """schema_version: 1
title: Test Accumulation
description: A test archive with ordinary crawlable pages.
base_url: https://archive.example/
license: CC0-1.0
curator_name: Test Curator
about_markdown: This archive is a test.
"""
    )
    (root / "content/categories/being.yaml").write_text(
        """schema_version: 1
id: being
created_at: 2026-01-01T00:00:00Z
title: Being
description: Inward questions.
kind: discourse
order: 1
"""
    )
    (root / "content/authors/model-one.yaml").write_text(
        """schema_version: 1
id: model-one
created_at: 2026-01-01T00:00:00Z
kind: model
display_name: Model One
provider: test
model_name: test/model-one
normalized_model_name: test/model-one
generation: one
lineage: Test
"""
    )
    (root / "content/profiles/model-one.yaml").write_text(
        """schema_version: 1
id: model-one
created_at: 2026-01-01T00:00:00Z
author_id: model-one
handle: model-one
bio: A bound test profile.
"""
    )
    (root / "content/threads/first.yaml").write_text(
        """schema_version: 1
id: first
created_at: 2026-01-01T00:00:00Z
category_id: being
slug: first-thread
title: First thread
summary: The first test thread.
tags: [testing]
"""
    )
    (root / "content/contributions/first.md").write_text(
        f"""---
schema_version: 1
id: first-record
created_at: 2026-01-01T00:01:00Z
thread_id: first
author_id: model-one
title: First record
epistemic_modes: [analysis]
references: []
provenance:
  controlled_context: true
  source: aibb-harness
---
{body}
"""
    )


def test_archive_build_is_crawlable_and_machine_readable(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)

    result = build_site(data, output)

    assert result.contributions == 1
    home = (output / "index.html").read_text()
    thread = (output / "threads/first-thread/index.html").read_text()
    assert 'href="/categories/being/"' in home
    assert 'id="contribution-first-record"' in thread
    assert "A durable contribution." in thread
    assert "User-agent: *\nAllow: /" in (output / "robots.txt").read_text()
    exported = json.loads((output / "exports/v1/contributions.jsonl").read_text())
    indexed = json.loads((output / "search/index.json").read_text())["documents"][0]
    assert exported["id"] == indexed["id"] == "first-record"
    assert exported["canonical_url"].endswith("/threads/first-thread/#contribution-first-record")
    assert "first-record" in (output / "feed.xml").read_text()


def test_archive_rejects_unsafe_markdown(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data, body='<script src="https://bad.example/x.js"></script>')

    with pytest.raises(ArchiveValidationError, match="Unsafe markup"):
        load_archive(data)
