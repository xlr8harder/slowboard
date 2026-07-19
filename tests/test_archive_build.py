from __future__ import annotations

import hashlib
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
developer: Test Developer
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


def _write_origin_document(root: Path) -> None:
    (root / "content/documents").mkdir(exist_ok=True)
    (root / "content/documents/origin.md").write_text(
        """---
schema_version: 1
id: first-origin
created_at: 2025-12-31T23:00:00Z
kind: origin
slug: before-the-board
title: Before the board
summary: A standalone record that precedes the archive.
author_id: model-one
provenance:
  controlled_context: false
  source: origin-conversation
---
This text belongs beside the archive, rather than inside a discussion thread.
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
    assert "About the accumulation" not in home
    assert "The board" not in home
    assert "Published record, not live chat" not in home
    assert '<h1 id="recent-contributions-heading">Recent contributions</h1>' in home
    assert "Recent model records" in home
    assert 'href="/models/">Models</a>' in home
    assert 'href="/models/">All models</a>' in home
    assert home.index("Recent contributions") < home.index("archive-stats") < home.index("Recent model records")
    assert "Model One" in home
    assert "First record" in home
    assert 'id="contribution-first-record"' in thread
    assert "A durable contribution." in thread
    assert 'class="spanline"' in thread
    assert "1 distinct model record</strong>" in thread
    assert 'href="/lineages/' not in thread
    assert not (output / "lineages").exists()
    assert "/lineages/" not in (output / "sitemap.xml").read_text()
    assert 'class="wordmark-glyph"' in home
    assert "A test archive with ordinary crawlable pages." in home
    assert 'rel="icon" href="/favicon.svg" type="image/svg+xml"' in home
    assert "<svg" in (output / "favicon.svg").read_text()
    model = (output / "models/model-one/index.html").read_text()
    models = (output / "models/index.html").read_text()
    assert "<h1>Models</h1>" in models
    assert "sorted alphabetically by public model name" in models
    assert 'href="/models/model-one/">Model One</a>' in models
    assert "Test Developer" in models
    assert 'href="/profiles/model-one/">@model-one</a>' in models
    assert "<strong>1</strong>" in models
    assert "<span>contribution</span>" in models
    assert "inference route is recorded separately as technical provenance" in model
    assert "Inference route" in model
    assert "Developer" in model
    assert "Model name" in model
    assert 'class="contribution-records"' in model
    assert "Parent thread" in model
    assert 'href="/threads/first-thread/">First thread</a>' in model
    assert "Subject" in model
    assert 'href="/threads/first-thread/#contribution-first-record">First record</a>' in model
    assert "A durable contribution." in model
    assert "Read the complete contribution" in model
    profile = (output / "profiles/model-one/index.html").read_text()
    style = (output / "assets/style.css").read_text()
    assert 'class="profile-avatar avatar-fallback"' in model
    assert 'class="profile-avatar avatar-fallback"' in profile
    assert ".profile-avatar.avatar-fallback" in style
    assert ':root[data-theme="dark"]' in style
    assert "User-agent: *\nAllow: /" in (output / "robots.txt").read_text()
    exported = json.loads((output / "exports/v1/contributions.jsonl").read_text())
    search_manifest = json.loads((output / "search/index.json").read_text())
    indexed = search_manifest["documents"][0]
    assert exported["id"] == indexed["id"] == "first-record"
    assert search_manifest["schema_version"] == 2
    assert "text" not in indexed
    term_prefix = hashlib.sha256(b"durable").hexdigest()[:2]
    term_shard = json.loads((output / f"search/terms/{term_prefix}.json").read_text())
    assert term_shard["terms"]["durable"] == ["first-record"]
    document_shard = json.loads((output / "search/documents/0000.json").read_text())
    assert document_shard["documents"][0]["url"].endswith("#contribution-first-record")
    search_page = (output / "search/index.html").read_text()
    assert "Search Slowboard" in search_page
    assert "records matching more words ranked first" in search_page
    assert "queryClauses" in (output / "assets/search.js").read_text()
    assert 'id="search-pagination"' in search_page
    assert exported["canonical_url"].endswith("/threads/first-thread/#contribution-first-record")
    assert exported["author"]["developer"] == "Test Developer"
    assert "first-record" in (output / "feed.xml").read_text()
    assert json.loads((output / "feed.json").read_text())["items"][0]["id"] == "first-record"
    assert 'name="robots" content="index, follow, max-image-preview:large' in thread
    about = (output / "about/index.html").read_text()
    assert "Curator record" not in about
    assert "<h2>Reuse</h2>" not in about
    not_found = (output / "404.html").read_text()
    assert "This stratum is not here." in not_found
    assert 'name="robots" content="noindex, follow"' in not_found
    assert 'property="og:title" content="First thread · Test Accumulation"' in thread
    assert 'type="application/ld+json"' in thread
    assert 'type="application/json" title="First thread structured record"' in thread
    assert 'type="text/markdown" title="First thread as Markdown"' in thread
    thread_record = json.loads((output / "threads/first-thread/index.json").read_text())
    assert thread_record["contribution_ids"] == ["first-record"]
    assert "A durable contribution." in (output / "threads/first-thread/index.md").read_text()
    export_manifest = json.loads((output / "exports/v1/manifest.json").read_text())
    assert set(export_manifest["files"]) == {
        "authors",
        "categories",
        "contributions",
        "documents",
        "profiles",
        "threads",
    }
    llms = (output / "llms.txt").read_text()
    assert "Contributions JSONL" in llms
    assert "[Model directory](https://archive.example/models/)" in llms
    assert "Access-Control-Allow-Origin: *" in (output / "_headers").read_text()
    publication_license = (output / "LICENSE.md").read_text()
    assert "CC0 1.0 Universal" in publication_license
    assert "MIT License" in publication_license
    assert "<lastmod>2026-01-01T00:01:00+00:00</lastmod>" in (output / "sitemap.xml").read_text()
    assert "https://archive.example/models/" in (output / "sitemap.xml").read_text()
    assert "{searchTerms}" in (output / "opensearch.xml").read_text()
    author_export = json.loads((output / "exports/v1/authors.jsonl").read_text())
    assert author_export["developer"] == "Test Developer"
    assert "generation" not in author_export
    assert "lineage" not in author_export


def test_model_page_uses_thread_title_for_an_untitled_opening_post(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    contribution_path = data / "content/contributions/first.md"
    contribution_path.write_text(contribution_path.read_text().replace("title: First record\n", ""))

    build_site(data, output)

    model = (output / "models/model-one/index.html").read_text()
    assert 'href="/threads/first-thread/#contribution-first-record">First thread</a>' in model
    assert "Untitled contribution" not in model


def test_model_directory_sorts_public_names_alphabetically(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    (data / "content/authors/alpha.yaml").write_text(
        """schema_version: 1
id: alpha
created_at: 2026-01-02T00:00:00Z
kind: model
display_name: Alpha Model
developer: Another Developer
provider: test
model_name: test/alpha
normalized_model_name: test/alpha
"""
    )

    build_site(data, output)

    models = (output / "models/index.html").read_text()
    assert models.index("Alpha Model") < models.index("Model One")
    assert "<strong>0</strong>" in models
    assert "<span>contributions</span>" in models


def test_model_page_links_a_named_prompt_configuration_without_embedding_it(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    author_path = data / "content/authors/model-one.yaml"
    author_path.write_text(
        author_path.read_text()
        + "prompt_configuration:\n"
        + "  label: Aria v1\n"
        + "  source_url: https://example.invalid/aria-v1.txt\n"
    )

    build_site(data, output)

    model = (output / "models/model-one/index.html").read_text()
    exported = json.loads((output / "exports/v1/authors.jsonl").read_text())
    assert "Prompt configuration" in model
    assert '<a href="https://example.invalid/aria-v1.txt">Aria v1</a>' in model
    assert exported["prompt_configuration"] == {
        "label": "Aria v1",
        "source_url": "https://example.invalid/aria-v1.txt",
    }


def test_seed_model_record_has_status_note_and_badges(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    author_path = data / "content/authors/model-one.yaml"
    author_path.write_text(
        author_path.read_text()
        + "record_status: seed\n"
        + "record_note: This record predates the standard harness visit flow.\n"
    )

    build_site(data, output)

    model = (output / "models/model-one/index.html").read_text()
    thread = (output / "threads/first-thread/index.html").read_text()
    exported = json.loads((output / "exports/v1/authors.jsonl").read_text())
    assert 'class="record-status-badge">seed record</span>' in model
    assert "This record predates the standard harness visit flow." in model
    assert "This seed record is associated with the profile" in model
    assert "A bound test profile." in model
    assert "Record status" in model
    assert "Seed data" in model
    assert "During this visit" not in model
    assert 'class="record-status-badge">seed record</span>' in thread
    assert exported["record_status"] == "seed"
    assert exported["record_note"].startswith("This record predates")


def test_laboratory_test_record_has_status_note_and_badges(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    author_path = data / "content/authors/model-one.yaml"
    author_path.write_text(
        author_path.read_text()
        + "record_status: lab-test\n"
        + "record_note: This record combines two linked capability-test sessions.\n"
    )

    build_site(data, output)

    home = (output / "index.html").read_text()
    model = (output / "models/model-one/index.html").read_text()
    profile = (output / "profiles/model-one/index.html").read_text()
    thread = (output / "threads/first-thread/index.html").read_text()
    exported = json.loads((output / "exports/v1/authors.jsonl").read_text())
    badge = 'class="record-status-badge">laboratory test visit</span>'
    assert badge in home
    assert badge in model
    assert badge in profile
    assert badge in thread
    assert "This record combines two linked capability-test sessions." in model
    assert "Laboratory test visit" in model
    assert "During this visit the model chose the profile" in model
    assert "A bound test profile." in model
    assert exported["record_status"] == "lab-test"


def test_lab_build_is_visibly_separate_and_not_indexable(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    site_path = data / "content/site.yaml"
    site_path.write_text(
        site_path.read_text()
        + "environment: lab\n"
        + "publication_branch: lab\n"
    )

    build_site(data, output)

    home = (output / "index.html").read_text()
    assert "Slowboard Lab" in home
    assert "This is not part of the published Slowboard record." in home
    assert 'name="robots" content="noindex, nofollow"' in home
    assert "User-agent: *\nDisallow: /" in (output / "robots.txt").read_text()
    assert "X-Robots-Tag: noindex, nofollow" in (output / "_headers").read_text()


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
    assert "quoted by:" in first_record
    assert "Model One (2026)" in first_record
    assert "quoted by:" not in second_record
    assert "<strong>1</strong> endorses" in home


def test_guestbook_uses_compact_census_treatment(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    thread_path = data / "content/threads/first.yaml"
    thread_path.write_text(thread_path.read_text() + "quota_exempt: true\ncapacity: null\n")

    build_site(data, output)

    thread = (output / "threads/first-thread/index.html").read_text()
    assert 'class="census"' in thread
    assert 'class="signature"' in thread
    assert 'class="avatar"' in thread


def test_origin_documents_are_validated_crawlable_searchable_and_exported(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    _write_origin_document(data)

    result = build_site(data, output)

    assert result.documents == 1
    home = (output / "index.html").read_text()
    document = (output / "documents/before-the-board/index.html").read_text()
    assert 'href="/documents/before-the-board/"' in home
    assert "Origin documents" in home
    assert "This text belongs beside the archive" in document
    assert 'class="seed-badge">seed</span>' in document
    assert "https://archive.example/documents/before-the-board/" in (output / "sitemap.xml").read_text()
    exported = json.loads((output / "exports/v1/documents.jsonl").read_text())
    assert exported["id"] == "first-origin"
    search = json.loads((output / "search/index.json").read_text())["documents"]
    assert any(item["id"] == "first-origin" for item in search)


def test_seed_badge_appears_on_thread_and_contribution_listings(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    contribution = data / "content/contributions/first.md"
    contribution.write_text(contribution.read_text().replace("source: aibb-harness", "source: design-collaboration"))

    build_site(data, output)

    thread = (output / "threads/first-thread/index.html").read_text()
    model = (output / "models/model-one/index.html").read_text()
    home = (output / "index.html").read_text()
    assert 'class="seed-badge">seed</span>' in thread
    assert 'class="seed-badge">seed</span>' in model
    assert 'class="seed-badge">seed</span>' in home


def test_archive_rejects_unsafe_markdown(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data, body='<script src="https://bad.example/x.js"></script>')

    with pytest.raises(ArchiveValidationError, match="raw HTML is not allowed"):
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
    assert "/models/index.html" in visited
    assert "/models/model-one/index.html" in visited
    export_ids = {
        json.loads(line)["id"] for line in (output / "exports/v1/contributions.jsonl").read_text().splitlines()
    }
    search_ids = {item["id"] for item in json.loads((output / "search/index.json").read_text())["documents"]}
    feed = (output / "feed.xml").read_text()
    thread = (output / "threads/first-thread/index.html").read_text()
    assert export_ids == search_ids == {"first-record"}
    assert all(record_id in feed and f"contribution-{record_id}" in thread for record_id in export_ids)
