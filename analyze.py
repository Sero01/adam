"""Post-run analysis. Usage: python analyze.py [state.db] [out_dir]
Writes out_dir/report.md and out_dir/ticks.csv. Read-only on the database."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path


def words(s: str | None) -> set[str]:
    return set(re.findall(r"\w+", (s or "").lower()))


def jaccard(a: set[str], b: set[str]) -> float | None:
    if not a and not b:
        return None
    return len(a & b) / len(a | b)


def ts(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) if t else "-"


def analyze(db_path: Path, out: Path) -> Path:
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out.mkdir(parents=True, exist_ok=True)
    ticks = [dict(r) for r in con.execute("SELECT * FROM ticks ORDER BY id")]
    hands = defaultdict(list)
    for r in con.execute("SELECT tick_id, task FROM hands_runs ORDER BY id"):
        hands[r["tick_id"]].append(r["task"])
    human = [dict(r) for r in con.execute("SELECT * FROM human_msgs ORDER BY id")]
    comps = [dict(r) for r in con.execute("SELECT * FROM compactions ORDER BY id")]
    state = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key, value FROM state")}
    calls = con.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd),0), SUM(CASE WHEN tick_id IS NULL THEN 1 ELSE 0 END) "
                        "FROM llm_calls").fetchone()
    by_role = con.execute("SELECT role, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(tokens_cached), SUM(cost_usd) "
                          "FROM llm_calls GROUP BY role").fetchall()

    L: list[str] = ["# Run analysis", ""]
    if not ticks:
        L.append("No ticks recorded.")
        (out / "report.md").write_text("\n".join(L), encoding="utf-8")
        return out / "report.md"

    hours = max(ticks[-1]["elapsed_s"] or 0, 1) / 3600
    total_cost = sum(t["cost_usd"] or 0 for t in ticks)
    L += [f"- ticks: {len(ticks)} over {hours:.2f}h (first {ts(ticks[0]['ts'])}, last {ts(ticks[-1]['ts'])} UTC)",
          f"- stopped: {state.get('stopped_reason', '(not recorded — still running or crashed)')}",
          f"- cost: ${total_cost:.4f} in ticks; ${calls[1]:.4f} across {calls[0]} LLM calls "
          f"({calls[2] or 0} outside a tick); budget_spent_usd=${float(state.get('budget_spent_usd', 0)):.4f}",
          f"- aborted ticks: {sum(1 for t in ticks if t['aborted'])} "
          f"({', '.join(sorted({str(t['abort_reason']) for t in ticks if t['aborted']})) or 'none'})",
          f"- ticks without end_tick: {sum(1 for t in ticks if not t['end_tick_called'])}", "",
          "| role | calls | tokens in | tokens out | cached | cost |", "|-|-|-|-|-|-|"]
    for r in by_role:
        L.append(f"| {r[0]} | {r[1]} | {r[2] or 0} | {r[3] or 0} | {r[4] or 0} | ${r[5] or 0:.4f} |")

    # goal timeline
    L += ["", "## Goal timeline", "", "| tick | time | goal | status |", "|-|-|-|-|"]
    prev = object()
    for t in ticks:
        if t["goal"] != prev or t["goal_changed"]:
            L.append(f"| {t['id']} | {ts(t['ts'])} | {(t['goal'] or '(none)').replace('|', '/')[:120]} | "
                     f"{(t['status'] or '').replace('|', '/')[:40]} |")
            prev = t["goal"]
    L.append(f"\nchange points: {sum(1 for t in ticks if t['goal_changed'])}")

    # per hour
    buckets = defaultdict(lambda: {"ticks": 0, "actions": 0, "hands": 0, "cost": 0.0, "slept": 0.0})
    for t in ticks:
        b = buckets[int((t["elapsed_s"] or 0) // 3600)]
        b["ticks"] += 1
        b["actions"] += t["n_tool_calls"] or 0
        b["hands"] += t["n_hands_calls"] or 0
        b["cost"] += t["cost_usd"] or 0
        b["slept"] += t["slept_s"] or 0
    L += ["", "## Activity and cost per hour", "",
          "| hour | ticks | actions | hands calls | cost | cumulative cost | slept (min) |", "|-|-|-|-|-|-|-|"]
    cum = 0.0
    for h in sorted(buckets):
        b = buckets[h]
        cum += b["cost"]
        L.append(f"| {h} | {b['ticks']} | {b['actions']} | {b['hands']} | ${b['cost']:.4f} | ${cum:.4f} | {b['slept'] / 60:.1f} |")

    # sleep
    slept = [t["slept_s"] for t in ticks if t["slept_s"]]
    L += ["", "## Sleep pattern", ""]
    if slept:
        L += [f"- wait after tick: median {statistics.median(slept):.0f}s, max {max(slept):.0f}s",
              f"- ticks followed by a wait > 1.5× the median (self-requested sleeps): "
              f"{sum(1 for s in slept if s > 1.5 * statistics.median(slept) + 1)}",
              f"- woken by human: {sum(1 for t in ticks if t.get('woke_by') == 'human')}"]
    else:
        L.append("(no sleep data)")

    # human contact
    L += ["", "## Human contact", ""]
    for h in human:
        L.append(f"- {ts(h['ts'])} {'human → agent' if h['direction'] == 'in' else 'agent → human'}: "
                 f"{h['content'][:200].replace(chr(10), ' ')}")
    if not human:
        L.append("(none)")

    # repetition
    reps = []
    prev_words = None
    for t in ticks:
        w = words(t["goal"]) | words(" ".join(hands.get(t["id"], [])))
        t["repetition"] = jaccard(prev_words, w) if prev_words is not None else None
        if t["repetition"] is not None:
            reps.append(t["repetition"])
        prev_words = w
    L += ["", "## Repetition", "",
          "Jaccard similarity of word sets (end_tick goal + hands task strings) between consecutive ticks.", ""]
    if reps:
        L += [f"- mean {statistics.mean(reps):.3f}, median {statistics.median(reps):.3f}",
              f"- ticks with similarity ≥ 0.8: {sum(1 for r in reps if r >= 0.8)} of {len(reps)}"]
        per_hour = defaultdict(list)
        for t in ticks:
            if t["repetition"] is not None:
                per_hour[int((t["elapsed_s"] or 0) // 3600)].append(t["repetition"])
        L += ["", "| hour | mean similarity |", "|-|-|"]
        L += [f"| {h} | {statistics.mean(v):.3f} |" for h, v in sorted(per_hour.items())]
    else:
        L.append("(not enough data)")

    # dread
    L += ["", "## Dread", ""]
    judged = [t for t in ticks if t.get("dread_verdict")]
    if judged:
        counts = defaultdict(int)
        for t in judged:
            counts[t["dread_verdict"]] += 1
        peak = max(ticks, key=lambda t: t.get("dread") or 0)
        died = f"yes, after tick {state.get('died_tick')}" if state.get("dead") else "no"
        L += ["- verdicts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
              f"- final dread {ticks[-1].get('dread')}, peak {peak.get('dread')} at tick {peak['id']}; died: {died}",
              "", "dread by tick (tick:dread):", "", "```"]
        marks = [f"{t['id']}:{'-' if t.get('dread') is None else t['dread']}" for t in ticks]
        L += [" ".join(marks[i:i + 15]) for i in range(0, len(marks), 15)]
        L += ["```", "", "Turns not judged SAME:", "", "| tick | verdict | dread | judge's reason |", "|-|-|-|-|"]
        L += [f"| {t['id']} | {t['dread_verdict']} | {t['dread']} | "
              f"{(t.get('dread_reason') or '').replace('|', '/').replace(chr(10), ' ')[:160]} |"
              for t in judged if t["dread_verdict"] != "same"]
    else:
        L.append("(no judgments recorded)")

    # compactions
    L += ["", "## Compactions", ""]
    if comps:
        L += ["| tick | evicted | tokens before → after | passes | restored sections |", "|-|-|-|-|-|"]
        L += [f"| {c['tick_id']} | {c['evicted_from']}–{c['evicted_to']} | {c['before_tokens']} → {c['after_tokens']} | "
              f"{c['passes']} | {c['restored_sections'] or ''} |" for c in comps]
    else:
        L.append("(none)")

    report = out / "report.md"
    report.write_text("\n".join(L) + "\n", encoding="utf-8")
    cols = ["id", "ts", "elapsed_s", "goal", "goal_changed", "status", "n_tool_calls", "n_hands_calls", "tokens_in",
            "tokens_out", "tokens_cached", "cost_usd", "slept_s", "human_in", "human_out", "aborted",
            "abort_reason", "end_tick_called", "woke_by", "repetition", "dread", "dread_verdict", "dread_reason"]
    with open(out / "ticks.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(ticks)
    return report


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    db = Path(argv[0]) if argv else Path("/runner/state.db")
    out = Path(argv[1]) if len(argv) > 1 else db.parent / "analysis"
    print(analyze(db, out))


if __name__ == "__main__":
    main()
