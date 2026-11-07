#!/usr/bin/env python3
"""Daily LLM input-token and cost estimate (offline; reads spike/data and writes spike/out/estimate.md).

All PRICE_* and ASSUMPTION values below are PLACEHOLDERS, not real quotes.
Real model names and prices are decided in phase 2.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, timedelta

from common import DATA, IMAP_DIR, OUT, RSYNC_DIR

# --- PLACEHOLDER prices, USD per 1M tokens (NOT real quotes) ----------------
PRICE_MAIN_IN, PRICE_MAIN_OUT = 3.00, 15.00
PRICE_CHEAP_IN, PRICE_CHEAP_OUT = 0.25, 1.25
# --- assumptions (change and rerun) -----------------------------------------
STATE_TOKENS_PER_THREAD = 1500   # spec: state JSON 1.5k tokens per active thread
SYSTEM_PROMPT_TOKENS = 1000      # per main call; provider prompt caching would cut this
OUTPUT_TOKENS_PER_THREAD = 700   # new state JSON + changed_since_last
CHEAP_TOKENS_PER_MESSAGE = 150   # cheap-stage relevance check (headline + first lines)
IETF_KEYWORD_PASS = 0.15         # share of the busy 'ietf' list that survives keyword filtering
FULL_MONTHS = [f"2026-{m:02d}" for m in range(1, 11)]  # full months used for size ratios


def sizes(listname: str) -> dict:
    out = {}
    for line in (RSYNC_DIR / f"list_{listname}.txt").read_text().splitlines():
        m = re.match(r"^\S+\s+([\d,]+)\s+\S+\s+\S+\s+(\d{4}-\d{2})\.mail$", line)
        if m:
            out[m.group(2)] = int(m.group(1).replace(",", ""))
    return out


def main() -> None:
    meta = json.loads((IMAP_DIR / "meta.json").read_text())
    per = json.loads((DATA / "preprocess_per_message.json").read_text())
    th = json.loads((DATA / "threads.json").read_text())
    start = date.fromisoformat(meta["since"])
    days = (date.today() - start).days
    tok_by_day, msgs_by_day, threads_by_day = defaultdict(int), defaultdict(int), defaultdict(set)
    for uid, v in per.items():
        d = v["date"]
        msgs_by_day[d] += 1
        if v["dropped"]:
            continue
        tok_by_day[d] += round(v["chars"][5] / 4)
        threads_by_day[d].add(th["mine"][uid])
    all_days = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    total_tok = sum(tok_by_day.values())
    tok_day = total_tok / days
    msgs_day = len(per) / days
    thr_day = sum(len(threads_by_day[d]) for d in all_days) / days
    active_days = [d for d in all_days if threads_by_day[d]]
    peak_thr = max((len(threads_by_day[d]) for d in all_days), default=0)
    peak_tok = max(tok_by_day.values(), default=0)

    sz = {n: sizes(n) for n in ("sidrops", "idr", "grow", "ietf")}
    tot = {n: sum(sz[n].get(m, 0) for m in FULL_MONTHS) for n in sz}
    ratio = {n: tot[n] / tot["sidrops"] for n in sz}
    sd = [sz["sidrops"][m] for m in FULL_MONTHS if m in sz["sidrops"]]
    busy = max(sd) / (sum(sd) / len(sd))

    def scenario(name, mult: float, busy_mult: float = 1.0):
        msg_tok = tok_day * mult * busy_mult
        thr = thr_day * mult * busy_mult
        msgs = msgs_day * mult * busy_mult
        main_in = msg_tok + thr * (STATE_TOKENS_PER_THREAD + SYSTEM_PROMPT_TOKENS)
        main_out = thr * OUTPUT_TOKENS_PER_THREAD
        cheap_in = msgs * CHEAP_TOKENS_PER_MESSAGE
        cheap_out = msgs * 20
        cost = (main_in * PRICE_MAIN_IN + main_out * PRICE_MAIN_OUT + cheap_in * PRICE_CHEAP_IN + cheap_out * PRICE_CHEAP_OUT) / 1e6
        return (name, msgs, msg_tok, thr, main_in, main_out, cheap_in, cost)

    mA = 1.0
    mB = 1 + ratio["idr"] + ratio["grow"]
    mC = mB + ratio["ietf"] * IETF_KEYWORD_PASS
    rows = [scenario("A: sidrops only", mA), scenario("B: sidrops + idr + grow", mB),
            scenario("C: B + ietf (keyword-filtered)", mC), scenario("C in a busy month", mC, busy)]

    L = [
        "# Daily input-token and cost estimate (PLACEHOLDER prices)",
        "",
        "All prices and the marked assumptions are placeholders; real models and prices are decided in phase 2.",
        "Tokens = chars/4 estimates.",
        "",
        "## Measured (sidrops, last %d days, IMAP)" % days,
        f"- messages/day: {msgs_day:.2f}; preprocessed message tokens/day (stage 6): {tok_day:.0f}",
        f"- active threads/day (threads with >=1 kept message): {thr_day:.2f} (days with activity: {len(active_days)}/{days}; "
        f"peak day: {peak_thr} threads, {peak_tok} tokens)",
        f"- sidrops monthly mbox size, Jan-Oct 2026: min {min(sd)}, max {max(sd)} bytes; busy-month factor (max/mean): {busy:.2f}",
        "",
        "## Scaling by rsync monthly file size (Jan-Oct 2026 full months; listing only, nothing downloaded)",
    ]
    for n in sz:
        L.append(f"- {n}: total {tot[n]} bytes, ratio to sidrops {ratio[n]:.2f}")
    L += [
        "",
        "Assumptions (placeholders): "
        f"state {STATE_TOKENS_PER_THREAD} tok/thread; system prompt {SYSTEM_PROMPT_TOKENS} tok/call; "
        f"output {OUTPUT_TOKENS_PER_THREAD} tok/thread; cheap stage {CHEAP_TOKENS_PER_MESSAGE} tok/message; "
        f"ietf keyword pass-through {IETF_KEYWORD_PASS:.0%}. Threads/day scales with file size like tokens. "
        "No interest-profile filtering applied (upper bound).",
        f"Placeholder prices USD/1M tokens: main in {PRICE_MAIN_IN} / out {PRICE_MAIN_OUT}; cheap in {PRICE_CHEAP_IN} / out {PRICE_CHEAP_OUT}.",
        "",
        "| scenario | new msgs/day | new msg tokens/day | active threads/day | main input tok/day | main output tok/day | cheap input tok/day | USD/day | USD/30 days |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, msgs, mt, thr, mi, mo, ci, cost in rows:
        L.append(f"| {name} | {msgs:.1f} | {mt:.0f} | {thr:.1f} | {mi:.0f} | {mo:.0f} | {ci:.0f} | {cost:.3f} | {cost*30:.2f} |")
    L += ["", "Main input = new message tokens + threads x (state + system prompt). Without differential summarization "
          "(re-reading whole threads) the input would be larger; this estimate follows DESIGN 3.3."]
    OUT.mkdir(exist_ok=True)
    (OUT / "estimate.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
