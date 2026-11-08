"""Adapter contract (specs/phase1.md, "SourceAdapter").

Adapters only fetch and parse. They return thread-linking hints (headers); assigning items to
conversations is done by ``sounding.conversations`` against the database.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol, runtime_checkable

from sounding.models import Source


class SourceError(Exception):
    """A fetch failed. ``code`` is a short fixed token; the message never carries server output."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ConversationHints:
    """Thread-linking headers, each Message-ID normalized with ``conversations.normalize_msgid``."""

    message_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class Item:
    external_id: str
    url: str | None
    title: str
    author_name: str | None  # display name; never an address
    author_key: str | None  # per-source identifier (mail: HMAC of the address)
    published_at: datetime  # timezone-aware
    body_text: str
    conversation_hints: ConversationHints | None = None
    is_auto: bool = False
    auto_reason: str | None = None


@dataclass(frozen=True)
class FetchResult:
    items: Sequence[Item]
    cursor: dict[str, Any] | None
    # Short tokens, e.g. "truncated", "not_found", "uidvalidity_changed", "skipped_unparseable:2".
    notes: tuple[str, ...] = ()
    # Set when the fetch stopped early (e.g. "disconnected"); items/cursor hold the progress made.
    error: str | None = None

    @property
    def not_found(self) -> bool:
        return "not_found" in self.notes


@runtime_checkable
class SourceAdapter(Protocol):
    kind: ClassVar[str]

    def fetch(self, source: Source, cursor: dict[str, Any] | None) -> FetchResult: ...


@runtime_checkable
class AncestorFetcher(Protocol):
    """Optional: look up specific messages by Message-ID (ancestor completion)."""

    def fetch_by_ids(self, source: Source, message_ids: Sequence[str]) -> list[Item]: ...
