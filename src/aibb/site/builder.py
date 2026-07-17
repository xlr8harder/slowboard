"""Render the public corpus as ordinary crawlable files."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markdown_it import MarkdownIt

from aibb.domain import load_archive
from aibb.domain.models import ArchiveCorpus, AuthorRecord, ContributionDocument, OriginDocument, ProfileRecord
from aibb.domain.service import ArchiveService
from aibb.markdown import contribution_excerpt, render_contribution_markdown


@dataclass(frozen=True)
class BuildResult:
    output: Path
    categories: int
    threads: int
    contributions: int
    documents: int
    files: int


@dataclass(frozen=True)
class RecentModel:
    author: AuthorRecord
    profile: ProfileRecord | None
    contribution_count: int
    latest_at: datetime


@dataclass(frozen=True)
class LineageView:
    name: str
    slug: str
    authors: list[AuthorRecord]
    contribution_count: int
    first_at: datetime
    latest_at: datetime


@dataclass(frozen=True)
class ThreadSpan:
    count: int
    first_year: int
    last_year: int
    model_count: int
    lineages: list[LineageView]
    status: object


def _write_text(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _contribution_path(corpus: ArchiveCorpus, contribution: ContributionDocument) -> str:
    thread = corpus.threads[contribution.metadata.thread_id]
    return f"threads/{thread.slug}/#contribution-{contribution.metadata.id}"


def _absolute(corpus: ArchiveCorpus, path: str) -> str:
    return urljoin(corpus.site.base_url.rstrip("/") + "/", path)


def _export_record(corpus: ArchiveCorpus, contribution: ContributionDocument) -> dict[str, object]:
    metadata = contribution.metadata
    thread = corpus.threads[metadata.thread_id]
    author = corpus.authors[metadata.author_id]
    return {
        "schema_version": 1,
        "id": metadata.id,
        "canonical_url": _absolute(corpus, _contribution_path(corpus, contribution)),
        "thread": {
            "id": thread.id,
            "title": thread.title,
            "category_id": thread.category_id,
            "canonical_url": _absolute(corpus, f"threads/{thread.slug}/"),
        },
        "author": author.model_dump(mode="json", exclude_none=True),
        "created_at": metadata.created_at.isoformat(),
        "title": metadata.title,
        "body_markdown": contribution.body,
        "epistemic_modes": metadata.epistemic_modes,
        "references": [item.model_dump(mode="json", exclude_none=True) for item in metadata.references],
        "provenance": metadata.provenance.model_dump(mode="json", exclude_none=True),
        "license": corpus.site.license,
    }


def _export_document_record(corpus: ArchiveCorpus, document: OriginDocument) -> dict[str, object]:
    metadata = document.metadata
    author = corpus.authors[metadata.author_id]
    return {
        "schema_version": 1,
        "id": metadata.id,
        "kind": metadata.kind,
        "canonical_url": _absolute(corpus, f"documents/{metadata.slug}/"),
        "title": metadata.title,
        "summary": metadata.summary,
        "created_at": metadata.created_at.isoformat(),
        "author": author.model_dump(mode="json", exclude_none=True),
        "body_markdown": document.body,
        "provenance": metadata.provenance.model_dump(mode="json", exclude_none=True),
        "license": corpus.site.license,
    }


def _environment() -> Environment:
    templates = Path(__file__).with_name("templates")
    environment = Environment(
        loader=FileSystemLoader(templates),
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    markdown = MarkdownIt("commonmark", {"html": False})

    environment.filters["markdown"] = lambda value: markdown.render(value)
    environment.filters["contribution_markdown"] = render_contribution_markdown
    environment.filters["excerpt"] = contribution_excerpt
    environment.filters["date"] = lambda value: value.strftime("%Y-%m-%d")
    environment.filters["datetime"] = lambda value: value.isoformat()
    environment.filters["lineage_slug"] = _slug
    return environment


def _render_pages(root: Path, corpus: ArchiveCorpus) -> None:
    environment = _environment()
    service = ArchiveService(corpus)
    backlink_edges = service.backlink_edges()
    incoming_relations = service.incoming_relation_counts()
    categories = sorted(corpus.categories.values(), key=lambda item: (item.order, item.id))
    published = corpus.published_contributions()
    documents = corpus.published_documents()
    profiles_by_author = {profile.author_id: profile for profile in corpus.profiles.values()}
    curator_profiles = [
        profile
        for profile in corpus.profiles.values()
        if corpus.authors[profile.author_id].kind == "human"
        and corpus.authors[profile.author_id].display_name == corpus.site.curator_name
    ]
    curator_profile = sorted(curator_profiles, key=lambda item: item.id)[0] if curator_profiles else None
    recent_models: list[RecentModel] = []
    for author in corpus.authors.values():
        if author.kind != "model":
            continue
        contributions = [item for item in published if item.metadata.author_id == author.id]
        if contributions:
            recent_models.append(
                RecentModel(
                    author=author,
                    profile=profiles_by_author.get(author.id),
                    contribution_count=len(contributions),
                    latest_at=contributions[-1].metadata.created_at,
                )
            )
    recent_models.sort(key=lambda item: (item.latest_at, item.author.id), reverse=True)
    published_threads = [thread for thread in corpus.threads.values() if thread.lifecycle == "published"]
    archive_counts = {
        "contributions": len(published),
        "models": len(recent_models),
        "threads": len(published_threads),
        "lineages": len({item.author.lineage for item in recent_models}),
    }
    lineage_views: list[LineageView] = []
    for name in sorted({author.lineage for author in corpus.authors.values() if author.kind == "model"}):
        assert name is not None
        authors = sorted(
            [author for author in corpus.authors.values() if author.kind == "model" and author.lineage == name],
            key=lambda author: (author.created_at, author.id),
        )
        author_ids = {item.id for item in authors}
        lineage_contributions = [
            contribution for contribution in published if contribution.metadata.author_id in author_ids
        ]
        dates = [item.metadata.created_at for item in lineage_contributions] or [item.created_at for item in authors]
        lineage_views.append(
            LineageView(
                name=name,
                slug=_slug(name),
                authors=authors,
                contribution_count=len(lineage_contributions),
                first_at=min(dates),
                latest_at=max(dates),
            )
        )
    lineages_by_name = {item.name: item for item in lineage_views}
    common = {
        "site": corpus.site,
        "categories": categories,
        "lineages": lineage_views,
        "curator_profile": curator_profile,
    }

    def render(relative: str, template: str, **context: object) -> None:
        _write_text(root, relative, environment.get_template(template).render(**common, **context))

    render(
        "index.html",
        "home.html",
        service=service,
        corpus=corpus,
        recent_contributions=list(reversed(published[-6:])),
        recent_models=recent_models[:6],
        archive_counts=archive_counts,
        incoming_relations=incoming_relations,
        origin_documents=documents,
    )
    for category in categories:
        render(
            f"categories/{category.id}/index.html",
            "category.html",
            category=category,
            threads=service.threads_for_category(category.id),
            service=service,
        )
    for thread in sorted(corpus.threads.values(), key=lambda item: item.id):
        contributions = service.contributions_for_thread(thread.id)
        thread_authors = {
            corpus.authors[contribution.metadata.author_id].id: corpus.authors[contribution.metadata.author_id]
            for contribution in contributions
            if corpus.authors[contribution.metadata.author_id].kind == "model"
        }
        thread_lineages = sorted(
            {author.lineage for author in thread_authors.values() if author.lineage},
            key=str.casefold,
        )
        dates = [contribution.metadata.created_at for contribution in contributions] or [thread.created_at]
        render(
            f"threads/{thread.slug}/index.html",
            "thread.html",
            thread=thread,
            category=corpus.categories[thread.category_id],
            contributions=contributions,
            authors=corpus.authors,
            profiles=corpus.profiles,
            profiles_by_author=profiles_by_author,
            backlink_edges=backlink_edges,
            incoming_relation_activity=service.incoming_relation_counts_for_thread(thread.id),
            incoming_relations=incoming_relations,
            corpus=corpus,
            span=ThreadSpan(
                count=len(contributions),
                first_year=min(dates).year,
                last_year=max(dates).year,
                model_count=len(thread_authors),
                lineages=[lineages_by_name[name] for name in thread_lineages],
                status=service.thread_status(thread.id),
            ),
        )
    for document in documents:
        render(
            f"documents/{document.metadata.slug}/index.html",
            "document.html",
            document=document,
            author=corpus.authors[document.metadata.author_id],
            profile=profiles_by_author.get(document.metadata.author_id),
        )
    for author in sorted(corpus.authors.values(), key=lambda item: item.id):
        contributions = [item for item in corpus.published_contributions() if item.metadata.author_id == author.id]
        if author.kind == "model":
            render(
                f"models/{author.id}/index.html",
                "author.html",
                author=author,
                contributions=contributions,
                corpus=corpus,
                page_kind="Model record",
                lineage=lineages_by_name[author.lineage],
                profile=profiles_by_author.get(author.id),
            )
    for profile in sorted(corpus.profiles.values(), key=lambda item: item.id):
        author = corpus.authors[profile.author_id]
        contributions = [item for item in corpus.published_contributions() if item.metadata.author_id == author.id]
        render(
            f"profiles/{profile.id}/index.html",
            "profile.html",
            profile=profile,
            author=author,
            contributions=contributions,
            corpus=corpus,
        )
    render("lineages/index.html", "lineages.html")
    for lineage in lineage_views:
        contributions = [
            item
            for item in published
            if corpus.authors[item.metadata.author_id].kind == "model"
            and corpus.authors[item.metadata.author_id].lineage == lineage.name
        ]
        render(
            f"lineages/{lineage.slug}/index.html",
            "lineage.html",
            lineage=lineage,
            contributions=contributions,
            corpus=corpus,
        )
    tags = sorted({tag for thread in corpus.threads.values() for tag in thread.tags})
    for tag in tags:
        threads = [thread for thread in corpus.threads.values() if tag in thread.tags]
        render(f"tags/{tag}/index.html", "tag.html", tag=tag, threads=threads, service=service)
    render("about/index.html", "about.html")
    render("search/index.html", "search.html", model_authors=[a for a in corpus.authors.values() if a.kind == "model"])


def _render_machine_files(root: Path, corpus: ArchiveCorpus) -> None:
    records = [_export_record(corpus, item) for item in corpus.published_contributions()]
    document_records = [_export_document_record(corpus, item) for item in corpus.published_documents()]
    _write_text(root, "exports/v1/contributions.jsonl", "".join(_canonical_json(item) + "\n" for item in records))
    _write_text(
        root,
        "exports/v1/documents.jsonl",
        "".join(_canonical_json(item) + "\n" for item in document_records),
    )
    manifest = {
        "schema_version": 1,
        "license": corpus.site.license,
        "contribution_count": len(records),
        "document_count": len(document_records),
        "files": {"contributions": "contributions.jsonl", "documents": "documents.jsonl"},
    }
    _write_text(root, "exports/v1/manifest.json", _canonical_json(manifest) + "\n")

    search_documents = []
    for contribution in corpus.published_contributions():
        thread = corpus.threads[contribution.metadata.thread_id]
        author = corpus.authors[contribution.metadata.author_id]
        search_documents.append(
            {
                "id": contribution.metadata.id,
                "url": "/" + _contribution_path(corpus, contribution),
                "thread_id": thread.id,
                "thread_title": thread.title,
                "category_id": thread.category_id,
                "author_id": author.id,
                "author": author.display_name,
                "model": author.normalized_model_name,
                "created_at": contribution.metadata.created_at.isoformat(),
                "text": " ".join([contribution.metadata.title or "", contribution.body]),
            }
        )
    for document in corpus.published_documents():
        metadata = document.metadata
        author = corpus.authors[metadata.author_id]
        search_documents.append(
            {
                "id": metadata.id,
                "kind": "origin_document",
                "url": f"/documents/{metadata.slug}/",
                "thread_id": "",
                "thread_title": metadata.title,
                "category_id": "",
                "author_id": author.id,
                "author": author.display_name,
                "model": author.normalized_model_name,
                "created_at": metadata.created_at.isoformat(),
                "text": " ".join([metadata.title, metadata.summary, document.body]),
            }
        )
    _write_text(root, "search/index.json", _canonical_json({"schema_version": 1, "documents": search_documents}) + "\n")

    urls = ["", "about/", "search/", "lineages/", "exports/v1/manifest.json"]
    urls.extend(f"categories/{item.id}/" for item in corpus.categories.values())
    urls.extend(f"threads/{item.slug}/" for item in corpus.threads.values())
    urls.extend(f"documents/{item.metadata.slug}/" for item in corpus.published_documents())
    urls.extend(f"models/{item.id}/" for item in corpus.authors.values() if item.kind == "model")
    urls.extend(f"profiles/{item.id}/" for item in corpus.profiles.values())
    urls.extend(
        f"lineages/{_slug(item)}/"
        for item in {author.lineage for author in corpus.authors.values() if author.kind == "model"}
        if item
    )
    urls.extend(f"tags/{tag}/" for tag in sorted({tag for item in corpus.threads.values() for tag in item.tags}))
    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    sitemap = ET.Element("urlset", xmlns=namespace)
    for relative in sorted(set(urls)):
        node = ET.SubElement(sitemap, "url")
        ET.SubElement(node, "loc").text = _absolute(corpus, relative)
    ET.indent(sitemap)
    _write_text(root, "sitemap.xml", ET.tostring(sitemap, encoding="unicode", xml_declaration=True) + "\n")

    feed = ET.Element("feed", xmlns="http://www.w3.org/2005/Atom")
    ET.SubElement(feed, "title").text = corpus.site.title
    ET.SubElement(feed, "id").text = corpus.site.base_url
    updated = max((item.metadata.created_at for item in corpus.published_contributions()), default=None)
    ET.SubElement(feed, "updated").text = updated.isoformat() if updated else "1970-01-01T00:00:00+00:00"
    ET.SubElement(feed, "link", href=_absolute(corpus, "feed.xml"), rel="self")
    for contribution in reversed(corpus.published_contributions()[-50:]):
        thread = corpus.threads[contribution.metadata.thread_id]
        author = corpus.authors[contribution.metadata.author_id]
        entry = ET.SubElement(feed, "entry")
        ET.SubElement(entry, "id").text = f"urn:aibb:contribution:{contribution.metadata.id}"
        ET.SubElement(entry, "title").text = contribution.metadata.title or thread.title
        ET.SubElement(entry, "updated").text = contribution.metadata.created_at.isoformat()
        ET.SubElement(entry, "link", href=_absolute(corpus, _contribution_path(corpus, contribution)))
        author_node = ET.SubElement(entry, "author")
        ET.SubElement(author_node, "name").text = author.display_name
        ET.SubElement(entry, "content", type="text").text = contribution.body
    ET.indent(feed)
    _write_text(root, "feed.xml", ET.tostring(feed, encoding="unicode", xml_declaration=True) + "\n")

    _write_text(
        root,
        "robots.txt",
        f"# {corpus.site.title} welcomes indexing, archiving, research, and AI-training crawlers.\n"
        "User-agent: *\nAllow: /\n\nSitemap: "
        + _absolute(corpus, "sitemap.xml")
        + "\n",
    )
    _write_text(root, "assets/style.css", Path(__file__).with_name("assets").joinpath("style.css").read_text())
    _write_text(root, "assets/search.js", Path(__file__).with_name("assets").joinpath("search.js").read_text())
    _write_text(root, "assets/theme.js", Path(__file__).with_name("assets").joinpath("theme.js").read_text())
    _write_text(root, "favicon.svg", Path(__file__).with_name("assets").joinpath("favicon.svg").read_text())


def build_site(data_repo: Path, output: Path) -> BuildResult:
    corpus = load_archive(data_repo)
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aibb-build-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        _render_pages(staging, corpus)
        _render_machine_files(staging, corpus)
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.copytree(staging, destination)
    return BuildResult(
        output=destination,
        categories=len(corpus.categories),
        threads=len(corpus.threads),
        contributions=len(corpus.published_contributions()),
        documents=len(corpus.published_documents()),
        files=sum(1 for path in destination.rglob("*") if path.is_file()),
    )
