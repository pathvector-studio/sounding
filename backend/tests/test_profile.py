from pathlib import Path

import pytest
from sqlalchemy import select

from sounding.models import Source
from sounding.profile import Profile, ProfileError, load_profile, sync_sources

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "profile.example.yaml"


def test_missing_file_tracks_nothing(tmp_path):
    p = load_profile(tmp_path / "nope.yaml")
    assert p.ietf_lists == [] and p.feed_urls == []
    (tmp_path / "empty.yaml").write_text("")
    assert load_profile(tmp_path / "empty.yaml").ietf_lists == []


def test_example_profile_is_valid():
    p = load_profile(EXAMPLE)
    assert p.ietf_lists == ["sidrops", "grow"] and p.feed_urls == ["https://example.com/feed.xml"]
    assert p.keywords.include and p.ietf.drafts


def test_names_are_normalized_and_deduplicated(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("ietf:\n  working_groups: [SIDROPS, idr]\n  extra_lists: [idr, ietf]\n")
    assert load_profile(f).ietf_lists == ["sidrops", "idr", "ietf"]


@pytest.mark.parametrize(
    "body, field",
    [
        ("ietf:\n  working_groups: ['bad name; DROP']\n", "ietf.working_groups.0"),
        ("feeds:\n  - url: ftp://example.com/x\n", "feeds.0.url"),
        ("unknown_key: 1\n", "unknown_key"),
    ],
)
def test_invalid_profile_names_the_field_not_the_value(tmp_path, body, field):
    f = tmp_path / "p.yaml"
    f.write_text(body)
    with pytest.raises(ProfileError) as exc:
        load_profile(f)
    assert field in str(exc.value) and "DROP" not in str(exc.value) and "ftp://" not in str(exc.value)


def test_not_a_mapping(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("- a\n- b\n")
    with pytest.raises(ProfileError):
        load_profile(f)


def test_sync_creates_disables_and_reenables(session):
    p = Profile.model_validate(
        {"ietf": {"working_groups": ["sidrops", "idr"]}, "feeds": [{"url": "https://example.com/feed.xml"}]}
    )
    sync_sources(session, p)
    rows = {(s.kind, s.key): s for s in session.scalars(select(Source))}
    assert set(rows) == {("ietf_mail", "sidrops"), ("ietf_mail", "idr"), ("rss", "https://example.com/feed.xml")}
    assert rows[("rss", "https://example.com/feed.xml")].url == "https://example.com/feed.xml"
    rows[("ietf_mail", "idr")].cursor = {"uidvalidity": 1, "last_uid": 5}
    sync_sources(session, Profile.model_validate({"ietf": {"working_groups": ["sidrops"]}}))
    session.expire_all()
    rows = {(s.kind, s.key): s for s in session.scalars(select(Source))}
    assert rows[("ietf_mail", "idr")].enabled is False and rows[("ietf_mail", "idr")].cursor == {"uidvalidity": 1, "last_uid": 5}
    assert rows[("rss", "https://example.com/feed.xml")].enabled is False and rows[("ietf_mail", "sidrops")].enabled
    sync_sources(session, p)
    session.expire_all()
    assert all(s.enabled for s in session.scalars(select(Source)))
