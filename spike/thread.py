#!/usr/bin/env python3
"""JWZ threading of the saved IMAP messages, compared with the server's THREAD REFERENCES.

Output: spike/out/thread.md (numbers, UIDs and categories only) and
spike/data/threads.json (uid -> thread id, offline input for estimate.py).
Reusable core: build_threads(messages) is independent of IMAP and of IETF.
"""
from __future__ import annotations

import re
from collections import defaultdict

from common import IMAP_DIR, DATA, OUT, load_imap_messages, msg_date, msgid, write_json

# --- IETF/list specific knowledge, kept as configuration -------------------
LIST_TAGS = ["sidrops"]  # kept for documentation; any leading [blob] is stripped
REPLY_PREFIXES = "re|fwd?|aw|sv|vs|tr|ref|antw|答复|回复|回覆|转发|轉寄"  # incl. localized MUA prefixes


def base_subject(subj: str, list_tags=LIST_TAGS) -> tuple[str, bool]:
    """Normalize a subject (RFC 5256-ish). Returns (base, was_reply_or_fwd)."""
    s = re.sub(r"\s+", " ", str(subj or "")).strip()
    changed = False
    while True:
        prev = s
        s = re.sub(r"(?i)\s*\(fwd\)\s*$", "", s)
        m = re.match(r"(?i)^\s*(" + REPLY_PREFIXES + r")(\[\d+\])?\s*[:：]\s*", s)
        if m:
            s = s[m.end():]
            changed = True
        # RFC 5256 strips any leading [blob] (list tags, "[Internet]" added by gateways, ...)
        m = re.match(r"^\s*\[[^\]]*\]\s*(?=\S)", s)
        if m:
            s = s[m.end():]
        if s == prev:
            break
    return s.strip().lower(), changed


class Container:
    __slots__ = ("id", "uid", "parent", "children")

    def __init__(self, id_):
        self.id, self.uid, self.parent, self.children = id_, None, None, []


def _is_ancestor(a: Container, b: Container) -> bool:
    while b is not None:
        if b is a:
            return True
        b = b.parent
    return False


def _link(parent: Container, child: Container) -> None:
    if child.parent is parent or parent is child or _is_ancestor(child, parent):
        return
    if child.parent is not None:
        child.parent.children.remove(child)
    child.parent = parent
    parent.children.append(child)


def ref_ids(msg) -> list:
    refs = re.findall(r"<([^>]+)>", str(msg.get("References", "")))
    if not refs:
        irt = re.findall(r"<([^>]+)>", str(msg.get("In-Reply-To", "")))
        refs = irt[-1:]
    return [r.strip().lower() for r in refs]


def build_threads(messages: dict, use_subject: bool = True) -> dict:
    """messages: uid -> email Message. Returns uid -> thread id (int)."""
    table: dict = {}
    by_uid: dict = {}
    for uid, msg in messages.items():
        mid = msgid(msg) or f"synthetic-{uid}"
        c = table.get(mid)
        if c is None or c.uid is not None:  # new, or duplicate id
            if c is not None:
                mid = f"{mid}#dup{uid}"
            c = table[mid] = Container(mid)
        c.uid = uid
        by_uid[uid] = c
        prev = None
        for r in ref_ids(msg):
            rc = table.setdefault(r, Container(r))
            if prev is not None and rc.parent is None:
                _link(prev, rc)
            prev = rc
        if prev is not None:
            _link(prev, c)  # JWZ: last reference becomes parent (overrides)
    roots = [c for c in table.values() if c.parent is None]

    def members(c, acc):
        if c.uid is not None:
            acc.append(c.uid)
        for ch in c.children:
            members(ch, acc)
        return acc

    root_members = [members(r, []) for r in roots]
    # union-find over roots for subject merging
    parent = list(range(len(roots)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if use_subject:
        subj_root: dict = {}
        for i, (r, mem) in enumerate(zip(roots, root_members)):
            if not mem:
                continue
            first = min(mem)
            # subject of the root message (or earliest descendant when the root is empty)
            u = r.uid if r.uid is not None else first
            s, _ = base_subject(messages[u].get("Subject"))
            if not s:
                continue
            if s in subj_root:
                parent[find(i)] = find(subj_root[s])
            else:
                subj_root[s] = i
    out: dict = {}
    for i, mem in enumerate(root_members):
        for u in mem:
            out[u] = find(i)
    return out


def parse_server_threads(text: str) -> dict:
    """Parse '(1 2 (3)(4 5))(6)' into uid -> thread index (top-level groups)."""
    out, depth, idx, num = {}, 0, -1, ""
    for ch in text + " ":
        if ch.isdigit():
            num += ch
            continue
        if num:
            out[int(num)] = idx
            num = ""
        if ch == "(":
            if depth == 0:
                idx += 1
            depth += 1
        elif ch == ")":
            depth -= 1
    return out


def groups(m: dict) -> dict:
    g = defaultdict(set)
    for u, t in m.items():
        g[t].add(u)
    return g


def main() -> None:
    imap = load_imap_messages()
    msgs = {u: m for u, (_, m) in imap.items()}
    mine = build_threads(msgs, use_subject=True)
    mine_nosub = build_threads(msgs, use_subject=False)
    server = parse_server_threads((IMAP_DIR / "thread_references.txt").read_text())
    common_uids = set(mine) & set(server)
    gm, gn, gs = groups(mine), groups(mine_nosub), groups(server)

    def match_pct(a: dict, ga: dict) -> tuple[int, float]:
        gb = gs
        ok = sum(1 for u in common_uids if ga[a[u]] == gb[server[u]])
        return ok, 100.0 * ok / max(1, len(common_uids))

    ok, pct = match_pct(mine, gm)
    ok_n, pct_n = match_pct(mine_nosub, gn)

    def disagreements(m_assign, gm_):
        bad = [u for u in sorted(common_uids) if gm_[m_assign[u]] != gs[server[u]]]
        seen, examples = set(), []
        cats: dict = defaultdict(int)
        for u in bad:
            key = (frozenset(gm_[m_assign[u]]), frozenset(gs[server[u]]))
            if key in seen:
                continue
            seen.add(key)
            mg, sg = key
            cat = classify(msgs, mg, sg, gn, mine_nosub, server)
            cats[cat] += 1
            if len(examples) < 10:
                examples.append((sorted(mg)[:6], sorted(sg)[:6], len(mg), len(sg), cat))
        return seen, examples, cats

    seen, examples, cat_count = disagreements(mine, gm)
    seen_n, examples_n, cat_count_n = disagreements(mine_nosub, gn)

    dates = {}
    for u, m in msgs.items():
        d = msg_date(m)
        dates[u] = d.date().isoformat() if d else None
    write_json(DATA / "threads.json", {"mine": {str(u): t for u, t in mine.items()}, "dates": {str(u): d for u, d in dates.items()}})

    sizes = sorted((len(v) for v in gm.values()), reverse=True)
    missing_refs = sum(1 for m in msgs.values() if not ref_ids(m))
    lines = [
        "# JWZ vs server THREAD=REFERENCES (sidrops, last 30 days) - numbers only",
        "",
        f"- messages: {len(msgs)}; server-threaded: {len(server)}",
        f"- threads: mine (JWZ + subject merge) {len(gm)}; mine without subject merge {len(gn)}; server {len(gs)}",
        f"- messages whose whole thread grouping matches the server: {ok}/{len(common_uids)} = {pct:.1f}% "
        f"(without subject merge: {ok_n} = {pct_n:.1f}%)",
        f"- thread size distribution (mine, top 10): {sizes[:10]}; singletons: {sum(1 for s in sizes if s == 1)}",
        f"- messages with no References/In-Reply-To: {missing_refs}",
        f"- disagreeing clusters: {len(seen)} -> categories {dict(cat_count)}",
        "",
        "## Disagreement examples (UIDs, up to 6 per side; no bodies)",
    ]
    for mu, su, nm, ns, cat in examples:
        lines.append(f"- {cat}: mine group size {nm} uids {mu}; server group size {ns} uids {su}")
    if not examples:
        lines.append("- none (after the subject normalization fix described in the README; before it, one gateway-tagged "
                     "'[Internet]' subject and one localized reply prefix kept 1 message out of a 36-message thread)")
    lines += ["", f"## Without the subject fallback: {len(seen_n)} disagreeing clusters -> {dict(cat_count_n)}",
              "(first 10; UIDs up to 6 per side)"]
    for mu, su, nm, ns, cat in examples_n:
        lines.append(f"- {cat}: mine size {nm} uids {mu}; server size {ns} uids {su}")
    OUT.mkdir(exist_ok=True)
    (OUT / "thread.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def classify(msgs, mg, sg, gn, mine_nosub, server) -> str:
    """Heuristic category for one (my group, server group) disagreement."""
    only_mine, only_srv = mg - sg, sg - mg
    subs = {base_subject(msgs[u].get("Subject"))[0] for u in (mg | sg)}
    if only_mine and not only_srv:
        # I merged more than the server
        return "mine merged more (server kept apart)" + (": subject-fallback" if len(subs) == 1 else ": other")
    if only_srv and not only_mine:
        if any(not ref_ids(msgs[u]) for u in only_srv):
            return "server merged more: member lacks References/In-Reply-To (missing headers)"
        if len(subs) > 1:
            return "server merged more: subject changed within thread"
        return "server merged more: other"
    return "partial overlap (cross-linking, e.g. cross-post or phantom parent)"


if __name__ == "__main__":
    main()
