"""RSS/Atom over HTTP (specs/phase1.md, "RSS/Atom").

Only the feed itself is fetched (never the article pages). The feed URL is the ``sources.key``.
Conditional GET with ``{"etag", "last_modified"}``; redirects are followed by hand (at most 5,
http/https only, every hop checked); the body is streamed and capped at 5 MiB.

FETCH_CONTACT goes into the User-Agent only. It is never logged and never put in an error, and
errors carry fixed tokens, never server output or the URL.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import struct_time
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from sounding.config import Settings
from sounding.identity import display_name_for
from sounding.models import Source
from sounding.sources.base import FetchResult, Item, SourceError
from sounding.text import html_to_text

log = logging.getLogger(__name__)

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 5 * 1024 * 1024
REPO_URL = "https://github.com/pathvector-studio/sounding"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def app_version() -> str:
    """``SOUNDING_VERSION`` if set, else the ``VERSION`` file next to the package or at the repo
    root (the Docker image copies only ``backend/``, so there it is "dev" unless the file or the
    variable is provided), else "dev"."""
    env = os.environ.get("SOUNDING_VERSION", "").strip()
    if env:
        return env
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[3]):  # <app>/VERSION, <repo>/VERSION
        try:
            text = (base / "VERSION").read_text(encoding="utf-8").strip()
        except (OSError, IndexError, UnicodeDecodeError):
            continue
        if text and len(text) <= 40 and text.isprintable():
            return text
    return "dev"


def user_agent(settings: Settings) -> str:
    contact = settings.fetch_contact.get_secret_value().strip() if settings.fetch_contact_is_set else ""
    parts = f"+{REPO_URL}; {contact}" if contact else f"+{REPO_URL}"
    return f"Sounding/{app_version()} ({parts})"


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise SourceError("unsupported_url")


def _to_datetime(value: struct_time | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _entry_body(entry: Any) -> str:
    content = entry.get("content") or []
    raw = (content[0].get("value") if content else None) or entry.get("summary") or ""
    if not raw:
        return ""
    # Plain-text content has no tags and passes through; markup is flattened.
    return html_to_text(raw)


def parse_entry(entry: Any, fetched_at: datetime) -> Item:
    title = html_to_text(entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip() or None
    published = _to_datetime(entry.get("published_parsed")) or _to_datetime(entry.get("updated_parsed"))
    ext = (entry.get("id") or "").strip() or link
    if not ext:
        seed = f"{title}\n{published.isoformat() if published else ''}"
        ext = "hash:" + hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:32]
    author = (entry.get("author") or "").strip()
    return Item(
        external_id=ext,
        url=link if link and urlsplit(link).scheme.lower() in ("http", "https") else None,
        title=title or "(untitled)",
        # An address-like author is masked by the same helper as mail senders. RSS has no stable
        # person id, so author_key stays None.
        author_name=(display_name_for(author) if "@" in author else author) or None,
        author_key=None,
        published_at=published or fetched_at,
        body_text=_entry_body(entry),
    )


class RssAdapter:
    kind: ClassVar[str] = "rss"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._settings = settings
        self._clock = clock
        self._client = httpx.Client(
            transport=transport,
            timeout=float(settings.external_timeout_seconds),
            follow_redirects=False,
            headers={
                "User-Agent": user_agent(settings),
                "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, url: str, headers: dict[str, str]) -> tuple[httpx.Response, bytes]:
        """GET following up to MAX_REDIRECTS redirects; returns (response, body)."""
        for hop in range(MAX_REDIRECTS + 1):
            _check_url(url)
            try:
                with self._client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in _REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        if not location:
                            raise SourceError(f"http_{resp.status_code}")
                        if hop == MAX_REDIRECTS:
                            raise SourceError("too_many_redirects")
                        url = urljoin(url, location)
                        continue
                    if resp.status_code == 304:
                        return resp, b""
                    if not 200 <= resp.status_code < 300:
                        raise SourceError(f"http_{resp.status_code}")
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                        raise SourceError("response_too_large")
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_BODY_BYTES:
                            raise SourceError("response_too_large")
                    return resp, bytes(buf)
            except SourceError:
                raise
            except httpx.TimeoutException:
                raise SourceError("timeout") from None
            except httpx.InvalidURL:
                raise SourceError("unsupported_url") from None
            except httpx.HTTPError:
                raise SourceError("connection_error") from None
        raise SourceError("too_many_redirects")  # unreachable; keeps the type checker calm

    def fetch(self, source: Source, cursor: dict[str, Any] | None) -> FetchResult:
        fetched_at = self._clock()
        headers: dict[str, str] = {}
        if cursor:
            if cursor.get("etag"):
                headers["If-None-Match"] = str(cursor["etag"])
            if cursor.get("last_modified"):
                headers["If-Modified-Since"] = str(cursor["last_modified"])
        resp, body = self._get(source.key, headers)
        if resp.status_code == 304:
            return FetchResult(items=[], cursor=dict(cursor) if cursor else {"etag": None, "last_modified": None})
        parsed = feedparser.parse(io.BytesIO(body))
        entries = list(parsed.entries)
        if not entries and (parsed.bozo or not parsed.get("version")):
            raise SourceError("parse_error")
        items = [parse_entry(e, fetched_at) for e in entries]
        notes = ("parse_warning",) if parsed.bozo else ()
        new_cursor = {"etag": resp.headers.get("etag"), "last_modified": resp.headers.get("last-modified")}
        return FetchResult(items=items, cursor=new_cursor, notes=notes)
