"""Ancestor completion: fetch parents that are outside the fetched window (specs/phase1.md).

Source-independent; it only needs an adapter with ``fetch_by_ids``. Parents found are stored with
``fetched_via='ancestor'`` and their own References are followed. The run is bounded by a number
of lookups and by the age of the parent. Ids that cannot be found (replies to other lists, say)
are returned so the caller can remember them and not search again.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from sounding.ingest import ingest_items
from sounding.models import Item as ItemRow
from sounding.models import Source
from sounding.sources.base import AncestorFetcher, Item, SourceError

log = logging.getLogger(__name__)

LOOKUP_BATCH = 20
MISSING_IDS_CAP = 5000


@dataclass
class AncestorResult:
    ingested: int = 0
    looked_up: int = 0
    skipped_old: int = 0
    events: int = 0
    missing_added: list[str] = field(default_factory=list)
    error: str | None = None


def referenced_ids(items: Iterable[Item]) -> list[str]:
    ids: list[str] = []
    for it in items:
        h = it.conversation_hints
        if not h:
            continue
        for mid in (*h.references, h.in_reply_to):
            if mid and mid != h.message_id and mid not in ids:
                ids.append(mid)
    return ids


def merge_missing(old: Sequence[str], added: Iterable[str]) -> list[str]:
    """Old ids plus new ones, no duplicates, only the most recent ``MISSING_IDS_CAP``."""
    merged = list(dict.fromkeys([*old, *added]))
    return merged[-MISSING_IDS_CAP:]


def _known(session: Session, source_id: int, ids: Sequence[str]) -> set[str]:
    if not ids:
        return set()
    return set(
        session.scalars(select(ItemRow.message_id).where(ItemRow.source_id == source_id, ItemRow.message_id.in_(list(ids))))
    )


def complete_ancestors(
    session: Session,
    source: Source,
    adapter: object,
    polled: Sequence[Item],
    *,
    missing_ids: Sequence[str],
    max_depth: int,
    max_age_days: int,
    now: datetime,
) -> AncestorResult:
    result = AncestorResult()
    if not isinstance(adapter, AncestorFetcher) or max_depth <= 0:
        return result
    skip = set(missing_ids)
    oldest = now - timedelta(days=max_age_days)
    pending = [m for m in referenced_ids(polled) if m not in skip]
    pending = [m for m in pending if m not in _known(session, source.id, pending)]
    seen = set(pending) | skip
    budget = max_depth
    while pending and budget > 0:
        batch, pending = pending[: min(LOOKUP_BATCH, budget)], pending[min(LOOKUP_BATCH, budget) :]
        budget -= len(batch)
        result.looked_up += len(batch)
        try:
            found = adapter.fetch_by_ids(source, batch)
        except SourceError as exc:
            result.error = exc.code  # incomplete lookup: do not record these ids as missing
            break
        found_ids = {f.conversation_hints.message_id for f in found if f.conversation_hints}
        result.missing_added.extend(m for m in batch if m not in found_ids)
        keep: list[Item] = []
        for f in found:
            if f.published_at < oldest:
                result.skipped_old += 1
                if f.conversation_hints and f.conversation_hints.message_id:
                    result.missing_added.append(f.conversation_hints.message_id)  # too old: do not look again
                continue
            keep.append(f)
        if keep:
            ing = ingest_items(session, source, keep, fetched_via="ancestor")
            result.ingested += len(ing.new)
            result.events += ing.events
            fresh = [m for m in referenced_ids(keep) if m not in seen]
            fresh = [m for m in fresh if m not in _known(session, source.id, fresh)]
            seen.update(fresh)
            pending.extend(fresh)
    if pending and budget <= 0:
        log.info("ancestor lookups for list %s stopped at the per-run limit", source.key)
    return result
