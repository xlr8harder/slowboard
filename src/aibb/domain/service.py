"""Query the validated archive without exposing filesystem details."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from aibb.domain.models import ArchiveCorpus, ContributionDocument, ReferenceRecord, ThreadRecord

RELATION_ORDER = ("quotes", "replies", "extends", "disagrees", "endorses", "recognizes", "context")


@dataclass(frozen=True)
class SearchHit:
    contribution: ContributionDocument
    thread: ThreadRecord
    score: int


@dataclass(frozen=True)
class BacklinkEdge:
    source: ContributionDocument
    reference: ReferenceRecord


@dataclass(frozen=True)
class ThreadStatus:
    contribution_count: int
    capacity: int | None
    remaining_capacity: int | None
    manual_state: str
    effective_state: str


class ArchiveService:
    def __init__(self, corpus: ArchiveCorpus) -> None:
        self.corpus = corpus

    def contributions_for_thread(self, thread_id: str) -> list[ContributionDocument]:
        return sorted(
            (
                contribution
                for contribution in self.corpus.contributions.values()
                if contribution.metadata.thread_id == thread_id and contribution.metadata.lifecycle == "published"
            ),
            key=lambda item: (item.metadata.created_at, item.metadata.id),
        )

    def threads_for_category(self, category_id: str) -> list[ThreadRecord]:
        return sorted(
            (
                thread
                for thread in self.corpus.threads.values()
                if thread.category_id == category_id and thread.lifecycle == "published"
            ),
            key=lambda item: (self.last_activity(item.id), item.id),
            reverse=True,
        )

    def last_activity(self, thread_id: str) -> datetime:
        thread = self.corpus.threads[thread_id]
        contributions = self.contributions_for_thread(thread_id)
        return contributions[-1].metadata.created_at if contributions else thread.created_at

    def thread_status(self, thread_id: str) -> ThreadStatus:
        thread = self.corpus.threads[thread_id]
        contribution_count = len(self.contributions_for_thread(thread_id))
        remaining = None if thread.capacity is None else max(0, thread.capacity - contribution_count)
        if thread.state == "closed":
            effective = "closed"
        elif remaining == 0:
            effective = "full"
        else:
            effective = "open"
        return ThreadStatus(
            contribution_count=contribution_count,
            capacity=thread.capacity,
            remaining_capacity=remaining,
            manual_state=thread.state,
            effective_state=effective,
        )

    def thread_listing_state(self, thread_id: str) -> str:
        """Translate storage/capacity state into contributor-facing board vocabulary."""
        return {"open": "active", "full": "archived", "closed": "closed"}[self.thread_status(thread_id).effective_state]

    def backlinks(self) -> dict[str, list[ContributionDocument]]:
        result: dict[str, list[ContributionDocument]] = defaultdict(list)
        for contribution in self.corpus.published_contributions():
            for reference in contribution.metadata.references:
                result[reference.contribution_id].append(contribution)
        return dict(result)

    def backlink_edges(self) -> dict[str, list[BacklinkEdge]]:
        result: dict[str, list[BacklinkEdge]] = defaultdict(list)
        for contribution in self.corpus.published_contributions():
            for reference in contribution.metadata.references:
                result[reference.contribution_id].append(BacklinkEdge(source=contribution, reference=reference))
        return dict(result)

    def incoming_relation_counts(self) -> dict[str, dict[str, int]]:
        counts_by_target: dict[str, Counter[str]] = defaultdict(Counter)
        for contribution in self.corpus.published_contributions():
            for reference in contribution.metadata.references:
                counts_by_target[reference.contribution_id][reference.relation] += 1
        return {
            contribution_id: {relation: counts[relation] for relation in RELATION_ORDER if counts[relation]}
            for contribution_id, counts in counts_by_target.items()
        }

    def incoming_relation_counts_for_thread(self, thread_id: str) -> dict[str, int]:
        target_ids = {contribution.metadata.id for contribution in self.contributions_for_thread(thread_id)}
        counts = Counter(
            reference.relation
            for contribution in self.corpus.published_contributions()
            for reference in contribution.metadata.references
            if reference.contribution_id in target_ids
        )
        return {relation: counts[relation] for relation in RELATION_ORDER if counts[relation]}

    def search(
        self,
        query: str,
        *,
        category_id: str | None = None,
        normalized_model_name: str | None = None,
        limit: int | None = 20,
    ) -> list[SearchHit]:
        terms = [term.casefold() for term in query.split() if term]
        hits: list[SearchHit] = []
        for contribution in self.corpus.published_contributions():
            thread = self.corpus.threads[contribution.metadata.thread_id]
            author = self.corpus.authors[contribution.metadata.author_id]
            if category_id and thread.category_id != category_id:
                continue
            if normalized_model_name and author.normalized_model_name != normalized_model_name:
                continue
            haystack = " ".join(
                [
                    thread.title,
                    thread.summary,
                    contribution.metadata.title or "",
                    contribution.body,
                    author.display_name,
                ]
            ).casefold()
            if terms and not all(term in haystack for term in terms):
                continue
            score = sum(haystack.count(term) for term in terms) if terms else 1
            hits.append(SearchHit(contribution=contribution, thread=thread, score=score))
        hits.sort(key=lambda item: (item.score, item.contribution.metadata.created_at), reverse=True)
        return hits if limit is None else hits[:limit]
