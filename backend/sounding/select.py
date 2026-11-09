"""Choose what goes into one digest and record why (spec phase 3, R1-R4).

Entry point: ``select_for_digest(session_factory, client, profile, *, digest_id, start, end, now)``.
The caller creates the ``digests`` row (item 2 of phase 3); this writes its ``selections`` rows, then
stores the summaries of the chosen RSS items. It never calls the LLM for conversations: their
"where it stands" was made earlier by the summarize job and is only read here.

Idempotency: selections for ``digest_id`` are deleted and recomputed in one transaction, so a rerun
(a rebuilt period) gives a clean result. Repeated LLM judgments and summaries cost nothing: the
client reuses an identical request and existing ``item_summaries`` rows are never regenerated.

Period: ``[start, end)``.
* Conversations: a human (non-auto) message with ``published_at`` in the period.
* RSS items: ``items.fetched_at`` in the period. Digest periods are contiguous, so every item is
  seen by exactly one digest. ``published_at`` would drop items the feed dates in the past (or without
  a date) and re-offer none of them, and a first fetch of a feed with old entries would flood the digest.

Conversation score (weighted sum; each component is stored in ``reasons_json``):
``W_MESSAGES * human messages in period + W_PARTICIPANTS * distinct author_key in period
+ W_KEYWORDS * distinct include keywords matched + W_STATE_CHANGES * changed issues``. A changed issue is
one whose status differs from the version before (new issues count); only counted when the latest
state version for the language was created in the period.

RSS score: rule hit -> ``distinct include keywords matched * feed weight``, decided by ``rule``;
borderline (no include and no exclude hit) -> asked of the cheap tier; yes -> ``feed weight *
LLM_YES_FACTOR``, decided by ``llm`` (a no is recorded with its reason, decided by ``llm``). Without an
LLM answer (none, budget, failure, cap) the item is excluded by ``rule`` and the reason says which.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sounding.llm import LLMClient, LLMError
from sounding.models import Conversation, ConversationState, Item, Selection, Source
from sounding.profile import Profile
from sounding.state import _term_re
from sounding.summaries import SummaryResult, judge_item, summarize_items

log = logging.getLogger(__name__)

W_MESSAGES = 1.0
W_PARTICIPANTS = 2.0
W_KEYWORDS = 3.0
W_STATE_CHANGES = 2.0
LLM_YES_FACTOR = 0.5  # a yes from the LLM scores below a single keyword hit
MAX_LLM_JUDGMENTS = 50  # per run, a runaway stop (the daily budget is the real limit)


@dataclass
class SelectionResult:
    conversation_ids: list[int] = field(default_factory=list)  # included, best first
    item_ids: list[int] = field(default_factory=list)  # included, best first
    counts: dict[str, Any] = field(default_factory=dict)
    summaries: SummaryResult = field(default_factory=SummaryResult)


@dataclass
class _Decision:
    kind: str
    target_id: int
    included: bool
    score: float
    reasons: dict[str, Any]
    decided_by: str
    sort: tuple[Any, ...] = ()


def _hits(terms: list[str], text: str) -> list[str]:
    return [t.strip() for t in terms if t.strip() and _term_re(t).search(text)]


def _draft_hits(profile: Profile, low: str) -> list[str]:
    return [p.strip() for p in profile.ietf.drafts if p.strip() and p.strip().lower() in low]


# --- conversations (R1) --------------------------------------------------------------------------

def _state_changes(session: Session, conversation_id: int, language: str, start: datetime, end: datetime) -> int:
    rows = list(
        session.scalars(
            select(ConversationState)
            .where(ConversationState.conversation_id == conversation_id, ConversationState.language == language)
            .order_by(ConversationState.version.desc())
            .limit(2)
        )
    )
    if not rows or not (start <= rows[0].created_at < end):
        return 0

    def statuses(row: ConversationState | None) -> dict[str, str]:
        if row is None:
            return {}
        return {" ".join(i["issue"].lower().split()): i["status"] for i in row.state_json.get("issues", [])}

    new, old = statuses(rows[0]), statuses(rows[1] if len(rows) > 1 else None)
    return sum(1 for k, v in new.items() if old.get(k) != v)


def _select_conversations(
    session: Session, profile: Profile, *, language: str, start: datetime, end: datetime
) -> list[_Decision]:
    wg = set(profile.ietf.working_groups)
    rows = session.execute(
        select(Conversation, Source.key)
        .join(Source, Source.id == Conversation.source_id)
        .where(
            Source.kind == "ietf_mail", Source.enabled.is_(True), Conversation.merged_into.is_(None),
            select(Item.id)
            .where(
                Item.conversation_id == Conversation.id, Item.is_auto.is_(False),
                Item.published_at >= start, Item.published_at < end,
            )
            .exists(),
        )
        .order_by(Conversation.id)
    ).all()
    if not rows:
        return []
    ids = [c.id for c, _ in rows]
    humans: dict[int, list[Item]] = {}
    for it in session.scalars(
        select(Item).where(Item.conversation_id.in_(ids), Item.is_auto.is_(False)).order_by(Item.published_at, Item.id)
    ):
        humans.setdefault(it.conversation_id, []).append(it)  # type: ignore[arg-type]

    out: list[_Decision] = []
    for conv, list_key in rows:
        items = humans.get(conv.id, [])
        in_period = [i for i in items if start <= i.published_at < end]
        text = "\n".join([conv.subject or "", *[i.body_text for i in items if i.body_text]])
        low = text.lower()
        drafts = _draft_hits(profile, low)
        include = _hits(profile.keywords.include, text)
        exclude = _hits(profile.keywords.exclude, text)
        in_wg = list_key in wg
        base: dict[str, Any] = {
            "list": list_key, "drafts": drafts, "include_keywords": include, "exclude_keywords": exclude,
        }
        if exclude and not drafts:
            rule = "keyword_exclude"
        elif in_wg:
            rule = "working_group"
        elif drafts:
            rule = "tracked_draft"
        elif include:
            rule = "list_keyword"  # an extra list: needs a draft or include keyword
        else:
            rule = "no_match"
        if exclude and drafts:
            base["exclude_overridden_by_draft"] = True
        admitted = rule not in ("keyword_exclude", "no_match")
        comps = {
            "messages": len(in_period),
            "participants": len({i.author_key for i in in_period if i.author_key}),
            "keywords": len(include),
            "state_changes": _state_changes(session, conv.id, language, start, end),
        }
        score = (
            W_MESSAGES * comps["messages"] + W_PARTICIPANTS * comps["participants"]
            + W_KEYWORDS * comps["keywords"] + W_STATE_CHANGES * comps["state_changes"]
        ) if admitted else 0.0
        reasons = {
            **base, "rule": rule, "components": comps,
            "weights": {
                "messages": W_MESSAGES, "participants": W_PARTICIPANTS, "keywords": W_KEYWORDS,
                "state_changes": W_STATE_CHANGES,
            },
        }
        out.append(
            _Decision("conversation", conv.id, admitted, score, reasons, "rule", (-score, -conv.last_message_at.timestamp(), -conv.id))
        )
    return out


# --- RSS items (R2) ------------------------------------------------------------------------------

def _select_items(
    session_factory: Callable[[], Session], client: LLMClient | None, profile: Profile, *,
    language: str, start: datetime, end: datetime,
) -> tuple[list[_Decision], dict[str, int]]:
    weights = {f.url: f.weight for f in profile.feeds}
    counts = {"rule_in": 0, "rule_out": 0, "llm_yes": 0, "llm_no": 0, "llm_unavailable": 0, "llm_cap": 0}
    with session_factory() as s:
        items = list(
            s.execute(
                select(Item, Source.key)
                .join(Source, Source.id == Item.source_id)
                .where(
                    Source.kind == "rss", Source.enabled.is_(True),
                    Item.fetched_at >= start, Item.fetched_at < end,
                )
                .order_by(Item.fetched_at.desc(), Item.id.desc())
            ).all()
        )
        s.expunge_all()
    out: list[_Decision] = []
    borderline: list[tuple[Item, float, dict[str, Any]]] = []
    for it, feed in items:
        weight = weights.get(feed, 1.0)
        text = f"{it.title}\n{it.body_text or ''}"
        include, exclude = _hits(profile.keywords.include, text), _hits(profile.keywords.exclude, text)
        base: dict[str, Any] = {"feed": feed, "weight": weight, "include_keywords": include, "exclude_keywords": exclude}
        tie = (-it.published_at.timestamp(), -it.id)
        if exclude:
            counts["rule_out"] += 1
            out.append(_Decision("item", it.id, False, 0.0, {**base, "rule": "keyword_exclude"}, "rule"))
        elif include:
            counts["rule_in"] += 1
            score = len(include) * weight
            out.append(_Decision("item", it.id, True, score, {**base, "rule": "keyword_include"}, "rule", (-score, *tie)))
        else:
            borderline.append((it, weight, base))

    calls = 0
    budget_stopped = False
    for it, weight, base in borderline:
        tie = (-it.published_at.timestamp(), -it.id)
        reasons = {**base, "rule": "borderline"}
        if client is None:
            counts["llm_unavailable"] += 1
            out.append(_Decision("item", it.id, False, 0.0, {**reasons, "excluded": "llm_disabled"}, "rule"))
            continue
        if budget_stopped:
            counts["llm_unavailable"] += 1
            out.append(_Decision("item", it.id, False, 0.0, {**reasons, "excluded": "llm_budget"}, "rule"))
            continue
        if calls >= MAX_LLM_JUDGMENTS:
            counts["llm_cap"] += 1
            out.append(_Decision("item", it.id, False, 0.0, {**reasons, "excluded": "llm_cap"}, "rule"))
            continue
        calls += 1
        try:
            j = judge_item(client, profile, it, language=language)
        except LLMError as exc:
            counts["llm_unavailable"] += 1
            budget_stopped = exc.code == "budget_exceeded"
            why = "llm_budget" if budget_stopped else f"llm_failed:{exc.code}"
            out.append(_Decision("item", it.id, False, 0.0, {**reasons, "excluded": why}, "rule"))
            continue
        reasons = {**reasons, "llm": {"fits": j.fits, "reason": j.reason, "call_id": j.call_id}}
        if j.fits:
            counts["llm_yes"] += 1
            score = weight * LLM_YES_FACTOR
            out.append(_Decision("item", it.id, True, score, reasons, "llm", (-score, *tie)))
        else:
            counts["llm_no"] += 1
            out.append(_Decision("item", it.id, False, 0.0, reasons, "llm"))
    return out, counts


# --- entry point -----------------------------------------------------------------------------------

def _cut(decisions: list[_Decision], limit: int, why: str) -> list[_Decision]:
    """Mark everything past the best ``limit`` admitted decisions as not included. Returns the included, best first."""
    admitted = sorted((d for d in decisions if d.included), key=lambda d: d.sort)
    for d in admitted[max(limit, 0):]:
        d.included = False
        d.reasons["cut"] = why
    return admitted[: max(limit, 0)]


def select_for_digest(
    session_factory: Callable[[], Session],
    client: LLMClient | None,
    profile: Profile,
    *,
    digest_id: int,
    start: datetime,
    end: datetime,
    now: datetime,
) -> SelectionResult:
    """Select conversations and RSS items for ``[start, end)``, record why, summarize the chosen items.

    ``client`` None means ``LLM_PROVIDER=none``: borderline items are excluded and no summaries are made.
    See the module docstring for the rules and for idempotency.
    """
    language = profile.profile.language
    with session_factory() as s:
        conv_decisions = _select_conversations(s, profile, language=language, start=start, end=end)
    item_decisions, item_counts = _select_items(session_factory, client, profile, language=language, start=start, end=end)

    conv_in = _cut(conv_decisions, profile.profile.digest.max_threads, "max_threads")
    item_in = _cut(item_decisions, profile.profile.digest.max_items, "max_items")

    with session_factory() as s:
        s.execute(delete(Selection).where(Selection.digest_id == digest_id))
        for d in [*conv_decisions, *item_decisions]:
            s.add(
                Selection(
                    digest_id=digest_id, target_kind=d.kind, target_id=d.target_id, included=d.included,
                    score=d.score, reasons_json=d.reasons, decided_by=d.decided_by, created_at=now,
                )
            )
        s.commit()

    result = SelectionResult(
        conversation_ids=[d.target_id for d in conv_in],
        item_ids=[d.target_id for d in item_in],
        counts={
            "conversation_candidates": len(conv_decisions),
            "conversations_included": len(conv_in),
            "item_candidates": len(item_decisions),
            "items_included": len(item_in),
            **{f"items_{k}": v for k, v in item_counts.items()},
        },
    )
    result.summaries = summarize_items(session_factory, client, result.item_ids, language=language)
    result.counts.update(
        summaries_created=result.summaries.created, summaries_existing=result.summaries.existing,
        summaries_skipped=result.summaries.skipped,
    )
    log.info("selection for digest %d: %s", digest_id, result.counts)
    return result

