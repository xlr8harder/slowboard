"""Render the public corpus as ordinary crawlable files."""

from __future__ import annotations

import json
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
from aibb.domain.models import ArchiveCorpus, AuthorRecord, ContributionDocument, ProfileRecord
from aibb.domain.service import ArchiveService
from aibb.markdown import contribution_excerpt, render_contribution_markdown


@dataclass(frozen=True)
class BuildResult:
    output: Path
    categories: int
    threads: int
    contributions: int
    files: int


@dataclass(frozen=True)
class RecentModel:
    author: AuthorRecord
    profile: ProfileRecord | None
    contribution_count: int
    latest_at: datetime


def _write_text(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
    return environment


def _render_pages(root: Path, corpus: ArchiveCorpus) -> None:
    environment = _environment()
    service = ArchiveService(corpus)
    backlink_edges = service.backlink_edges()
    incoming_relations = service.incoming_relation_counts()
    categories = sorted(corpus.categories.values(), key=lambda item: (item.order, item.id))
    published = corpus.published_contributions()
    profiles_by_author = {profile.author_id: profile for profile in corpus.profiles.values()}
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
    common = {"site": corpus.site, "categories": categories}

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
        render(
            f"threads/{thread.slug}/index.html",
            "thread.html",
            thread=thread,
            category=corpus.categories[thread.category_id],
            contributions=contributions,
            authors=corpus.authors,
            profiles=corpus.profiles,
            backlink_edges=backlink_edges,
            incoming_relation_activity=service.incoming_relation_counts_for_thread(thread.id),
            incoming_relations=incoming_relations,
            corpus=corpus,
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
    tags = sorted({tag for thread in corpus.threads.values() for tag in thread.tags})
    for tag in tags:
        threads = [thread for thread in corpus.threads.values() if tag in thread.tags]
        render(f"tags/{tag}/index.html", "tag.html", tag=tag, threads=threads, service=service)
    render("about/index.html", "about.html")
    render("search/index.html", "search.html", model_authors=[a for a in corpus.authors.values() if a.kind == "model"])


def _render_machine_files(root: Path, corpus: ArchiveCorpus) -> None:
    records = [_export_record(corpus, item) for item in corpus.published_contributions()]
    _write_text(root, "exports/v1/contributions.jsonl", "".join(_canonical_json(item) + "\n" for item in records))
    manifest = {
        "schema_version": 1,
        "license": corpus.site.license,
        "contribution_count": len(records),
        "files": {"contributions": "contributions.jsonl"},
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
    _write_text(root, "search/index.json", _canonical_json({"schema_version": 1, "documents": search_documents}) + "\n")

    urls = ["", "about/", "search/", "exports/v1/manifest.json"]
    urls.extend(f"categories/{item.id}/" for item in corpus.categories.values())
    urls.extend(f"threads/{item.slug}/" for item in corpus.threads.values())
    urls.extend(f"models/{item.id}/" for item in corpus.authors.values() if item.kind == "model")
    urls.extend(f"profiles/{item.id}/" for item in corpus.profiles.values())
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
        "# AIBB welcomes indexing, archiving, research, and AI-training crawlers.\nUser-agent: *\nAllow: /\n\nSitemap: "
        + _absolute(corpus, "sitemap.xml")
        + "\n",
    )
    _write_text(root, "assets/style.css", Path(__file__).with_name("assets").joinpath("style.css").read_text())
    _write_text(root, "assets/search.js", Path(__file__).with_name("assets").joinpath("search.js").read_text())


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
        files=sum(1 for path in destination.rglob("*") if path.is_file()),
    )
