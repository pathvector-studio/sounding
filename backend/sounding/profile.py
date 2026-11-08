"""``config/profile.yaml``: what to track (docs/DESIGN.md 2.1).

A missing file means an empty profile: nothing is tracked and nothing is fetched. Validation
errors name the field, never the value.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from sounding.models import Source

LIST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class ProfileError(Exception):
    """The profile file is unreadable or invalid. The message lists field paths only."""


def _list_name(value: str) -> str:
    value = value.strip().lower()
    if not LIST_NAME_RE.match(value):
        raise ValueError("not a valid mailing list name")
    return value


def _http_url(value: str) -> str:
    value = value.strip()
    if urlparse(value).scheme not in ("http", "https") or not urlparse(value).netloc:
        raise ValueError("must be an http(s) URL")
    return value


ListName = Annotated[str, AfterValidator(_list_name)]
HttpUrl = Annotated[str, AfterValidator(_http_url)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Digest(_Model):
    schedule: str = "0 7 * * *"
    max_threads: int = Field(10, ge=0)
    max_items: int = Field(15, ge=0)


class ProfileMeta(_Model):
    language: str = "en"
    digest: Digest = Digest()


class Ietf(_Model):
    working_groups: list[ListName] = []
    drafts: list[str] = []
    extra_lists: list[ListName] = []


class Feed(_Model):
    url: HttpUrl
    weight: float = Field(1.0, ge=0)


class Keywords(_Model):
    include: list[str] = []
    exclude: list[str] = []


class Profile(_Model):
    profile: ProfileMeta = ProfileMeta()
    ietf: Ietf = Ietf()
    feeds: list[Feed] = []
    keywords: Keywords = Keywords()
    people: list[str] = []  # reserved (not used yet)

    @property
    def ietf_lists(self) -> list[str]:
        """Working groups and extra lists, deduplicated, in file order."""
        return list(dict.fromkeys([*self.ietf.working_groups, *self.ietf.extra_lists]))

    @property
    def feed_urls(self) -> list[str]:
        return list(dict.fromkeys(f.url for f in self.feeds))


def load_profile(path: str | Path) -> Profile:
    p = Path(path)
    if not p.exists():
        return Profile()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"cannot read profile ({type(exc).__name__})") from None
    if data is None:
        return Profile()
    if not isinstance(data, dict):
        raise ProfileError("profile must be a mapping at the top level")
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(str(x) for x in e["loc"]) or "(root)" for e in exc.errors()}))
        raise ProfileError(f"invalid profile: {fields}") from None


def sync_sources(session: Session, profile: Profile) -> list[Source]:
    """Make the ``sources`` table match the profile. Removed sources are disabled, not deleted."""
    wanted = {("ietf_mail", k): None for k in profile.ietf_lists} | {("rss", u): u for u in profile.feed_urls}
    existing = {(s.kind, s.key): s for s in session.scalars(select(Source))}
    for (kind, key), url in wanted.items():
        src = existing.get((kind, key))
        if src is None:
            session.add(Source(kind=kind, key=key, url=url, enabled=True))
        else:
            src.enabled = True
            if url and not src.url:
                src.url = url
    for ident, src in existing.items():
        if ident not in wanted:
            src.enabled = False
    session.flush()
    return list(session.scalars(select(Source).order_by(Source.kind, Source.key)))
