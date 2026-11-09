"""Helpers shared by everything that shows a digest to a person (web pages, Atom feed, email, webhook).

Every URL that comes from a feed or a mail is passed through ``clean_url`` so a ``javascript:`` link never
leaves the program, and strings are passed through ``mask_addresses`` so no mail address does.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from sounding.text import mask_addresses

_CONTROL = re.compile(r"[\x00-\x20\x7f]")


def clean_url(url: Any) -> str | None:
    """The URL if it is an absolute http(s) URL with a host and no whitespace/control characters, else None."""
    if not isinstance(url, str) or not url or _CONTROL.search(url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def mask_deep(value: Any) -> Any:
    """Deep copy with every string passed through ``mask_addresses`` (defence in depth; stored text is already masked)."""
    if isinstance(value, str):
        return mask_addresses(value)
    if isinstance(value, list):
        return [mask_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_deep(v) for k, v in value.items()}
    return value


def clean_body(body: dict[str, Any]) -> dict[str, Any]:
    """A digest body with unsafe URLs removed and addresses masked."""
    out = mask_deep(body or {})
    for c in out.get("conversations", []):
        c["web_url"] = clean_url(c.get("web_url"))
        c["latest_url"] = clean_url(c.get("latest_url"))
    for e in out.get("events", []):
        e["url"] = clean_url(e.get("url"))
    for i in out.get("items", []):
        i["url"] = clean_url(i.get("url"))
    return out
