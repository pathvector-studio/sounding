#!/usr/bin/env python3
"""IMAP (last 30 days) vs rsync mbox comparison. Writes spike/out/compare.md (numbers only)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from common import IMAP_DIR, OUT, RSYNC_DIR, load_imap_messages, load_mbox_messages, msg_date, msgid


def norm_hdr(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def body_text(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        return (part.get_content() if part else "").replace("\r\n", "\n").strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> None:
    meta = json.loads((IMAP_DIR / "meta.json").read_text())
    since = datetime.fromisoformat(meta["since"]).replace(tzinfo=timezone.utc)
    imap = load_imap_messages()
    imap_ids = {}
    for uid, (_, m) in imap.items():
        imap_ids.setdefault(msgid(m), []).append(uid)

    mbox_all = []
    per_file = {}
    for p in sorted(RSYNC_DIR.glob("*.mail")):
        msgs = load_mbox_messages(p)
        per_file[p.name] = len(msgs)
        mbox_all += msgs
    in_window = []
    nodate = 0
    for raw, m in mbox_all:
        d = msg_date(m)
        if d is None:
            nodate += 1
            continue
        # allow a one-day slack: IMAP SINCE uses internal date, not the Date header
        if d >= since - timedelta(days=1):
            in_window.append((raw, m))
    rs_ids = {}
    for raw, m in in_window:
        rs_ids.setdefault(msgid(m), []).append((raw, m))

    a, b = set(imap_ids) - {None}, set(rs_ids) - {None}
    both = a & b
    # header / body fidelity for overlapping messages
    hdr_diff = {"From": 0, "Subject": 0, "References": 0, "In-Reply-To": 0, "Date": 0}
    body_equal = body_diff_only_from_escape = body_diff = 0
    addr_obf_rsync = addr_obf_imap = 0
    from_line_escaped = 0
    for mid in both:
        im = imap[imap_ids[mid][0]][1]
        rm = rs_ids[mid][0][1]
        for h in hdr_diff:
            if norm_hdr(im.get(h)) != norm_hdr(rm.get(h)):
                hdr_diff[h] += 1
        bi, br = body_text(im), body_text(rm)
        if bi == br:
            body_equal += 1
        elif bi == re.sub(r"(?m)^>From ", "From ", br):
            body_diff_only_from_escape += 1
        elif re.sub(r"\s+", " ", bi) == re.sub(r"\s+", " ", br):
            body_equal += 1
        else:
            body_diff += 1
    for raw, m in in_window:
        f = str(m.get("From", ""))
        if "@" not in f and re.search(r" at ", f):
            addr_obf_rsync += 1
        if re.search(rb"(?m)^>From ", raw):
            from_line_escaped += 1
    for uid, (raw, m) in imap.items():
        f = str(m.get("From", ""))
        if "@" not in f and re.search(r" at ", f):
            addr_obf_imap += 1

    lines = [
        "# IMAP vs rsync (sidrops) - numbers only",
        "",
        f"- IMAP window start (SINCE): {meta['since']}; IMAP messages saved: {len(imap)}",
        f"- rsync messages per file: {per_file}; total {len(mbox_all)}; without parsable Date: {nodate}",
        f"- rsync messages in the same window (Date >= since - 1 day): {len(in_window)}",
        f"- distinct Message-IDs: IMAP {len(a)}, rsync-in-window {len(b)}",
        f"- IMAP messages without Message-ID: {len(imap_ids.get(None, []))}; rsync: {len(rs_ids.get(None, []))}",
        f"- Message-ID overlap: {len(both)}; only IMAP: {len(a - b)}; only rsync (in window): {len(b - a)}",
        f"- duplicate Message-IDs inside IMAP: {sum(1 for v in imap_ids.values() if len(v) > 1)}",
        f"- header differences among overlapping messages: {hdr_diff}",
        f"- decoded body identical (whitespace-insensitive): {body_equal}; differs only by '>From ' escaping: "
        f"{body_diff_only_from_escape}; other difference: {body_diff}",
        f"- messages with mbox '>From ' escaped lines: {from_line_escaped}",
        f"- From header looks obfuscated ('x at y', no '@'): rsync {addr_obf_rsync}, IMAP {addr_obf_imap}",
    ]
    OUT.mkdir(exist_ok=True)
    (OUT / "compare.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
