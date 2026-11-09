"""The Atom feed (spec phase 3, R9): the last 20 digests as plain-text entries (summaries and links only)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sounding.digest import render_digest_text
from sounding.models import Digest
from sounding.web.queries import clean_body

ATOM_NS = "http://www.w3.org/2005/Atom"
FEED_LIMIT = 20
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

ET.register_namespace("", ATOM_NS)


def _q(tag: str) -> str:
    return f"{{{ATOM_NS}}}{tag}"


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(parent: ET.Element, tag: str, text: str, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, _q(tag), attrs)
    el.text = _XML_BAD.sub("", text)
    return el


def build_feed(digests: list[Digest], *, base_url: str, feed_url: str, now: datetime | None = None) -> bytes:
    host = urlsplit(base_url).hostname or "localhost"
    now = now or datetime.now(UTC)
    root = ET.Element(_q("feed"))
    _text(root, "id", f"tag:{host},2026:sounding:feed")
    _text(root, "title", "Sounding")
    _text(root, "updated", _rfc3339(max((d.created_at for d in digests), default=now)))
    ET.SubElement(root, _q("link"), {"rel": "self", "type": "application/atom+xml", "href": feed_url})
    ET.SubElement(root, _q("link"), {"rel": "alternate", "type": "text/html", "href": base_url + "/"})
    for d in digests:
        body = clean_body(d.body_json or {})
        if not body.get("period"):  # a digest that is still being built
            continue
        p = body["period"]
        entry = ET.SubElement(root, _q("entry"))
        _text(entry, "id", f"tag:{host},{d.period_end.astimezone(UTC):%Y-%m-%d}:sounding:digest:{d.id}")
        _text(entry, "title", f"Sounding {p['start'].replace('T', ' ')[:16]} - {p['end'].replace('T', ' ')[:16]} ({p['tz']})")
        _text(entry, "updated", _rfc3339(d.created_at))
        ET.SubElement(entry, _q("link"), {"rel": "alternate", "type": "text/html", "href": f"{base_url}/digests/{d.id}"})
        _text(entry, "content", render_digest_text(body, d.language), type="text")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
