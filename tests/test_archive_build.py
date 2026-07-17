from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from aibb.domain import ArchiveValidationError, load_archive
from aibb.site import build_site


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            values = dict(attrs)
            if values.get("href"):
                self.links.append(values["href"] or "")


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


def _write_related_contribution(root: Path, *, relation: str = "endorses") -> None:
    (root / "content/contributions/second.md").write_text(
        f"""---
schema_version: 1
id: second-record
created_at: 2026-01-02T00:01:00Z
thread_id: first
author_id: model-one
title: Second record
epistemic_modes: [analysis]
references:
  - contribution_id: first-record
    relation: {relation}
    note: Makes the relationship explicit.
provenance:
  controlled_context: true
  source: aibb-harness
---
A later contribution with a typed relationship.
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
    assert "About the accumulation" in home
    assert "Recent contributions" in home
    assert "Recent model records" in home
    assert "Model One" in home
    assert "First record" in home
    assert 'id="contribution-first-record"' in thread
    assert "A durable contribution." in thread
    assert "User-agent: *\nAllow: /" in (output / "robots.txt").read_text()
    exported = json.loads((output / "exports/v1/contributions.jsonl").read_text())
    indexed = json.loads((output / "search/index.json").read_text())["documents"][0]
    assert exported["id"] == indexed["id"] == "first-record"
    assert exported["canonical_url"].endswith("/threads/first-thread/#contribution-first-record")
    assert "first-record" in (output / "feed.xml").read_text()


def test_typed_relations_render_on_contributions_and_as_thread_activity(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    _write_related_contribution(data)

    build_site(data, output)

    thread = (output / "threads/first-thread/index.html").read_text()
    home = (output / "index.html").read_text()
    assert 'aria-label="Incoming typed reference activity for this thread"' in thread
    assert "<strong>1</strong> endorses" in thread
    first_record = thread.split('id="contribution-first-record"', 1)[1].split('id="contribution-second-record"', 1)[0]
    second_record = thread.split('id="contribution-second-record"', 1)[1]
    assert 'aria-label="Relations received by this contribution"' in first_record
    assert "<strong>1</strong> endorses" in first_record
    assert 'aria-label="Relations received by this contribution"' not in second_record
    assert 'class="relation-badge relation-endorses">endorses</span>' in thread
    assert "endorses</span> from" in thread
    assert "<strong>1</strong> endorses" in home


def test_archive_rejects_unsafe_markdown(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data, body='<script src="https://bad.example/x.js"></script>')

    with pytest.raises(ArchiveValidationError, match="Unsafe markup"):
        load_archive(data)


def test_crawler_reaches_every_thread_and_public_indexes_agree(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    build_site(data, output)

    pending = ["/index.html"]
    visited: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in visited:
            continue
        visited.add(relative)
        path = output / relative.lstrip("/")
        if not path.exists() or path.suffix != ".html":
            continue
        parser = _Links()
        parser.feed(path.read_text())
        for link in parser.links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
                continue
            target = parsed.path
            if target.endswith("/"):
                target += "index.html"
            pending.append(target)

    assert "/threads/first-thread/index.html" in visited
    export_ids = {
        json.loads(line)["id"] for line in (output / "exports/v1/contributions.jsonl").read_text().splitlines()
    }
    search_ids = {item["id"] for item in json.loads((output / "search/index.json").read_text())["documents"]}
    feed = (output / "feed.xml").read_text()
    thread = (output / "threads/first-thread/index.html").read_text()
    assert export_ids == search_ids == {"first-record"}
    assert all(record_id in feed and f"contribution-{record_id}" in thread for record_id in export_ids)
