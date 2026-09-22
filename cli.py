"""Human channel. Run where the database lives:
    docker compose exec agent python cli.py <command>
(SQLite WAL over a Docker Desktop bind mount from the host is not safe, so the DB is in a volume.)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from agent.config import Config, load_config
from agent.memory import fmt_elapsed
from agent.runner import HEARTBEAT_S

HEARTBEAT_STALE_S = 4 * HEARTBEAT_S


def _db(cfg: Config) -> sqlite3.Connection:
    if not cfg.db_path.exists():
        sys.exit(f"no database at {cfg.db_path} (has the runner started?)")
    con = sqlite3.connect(cfg.db_path, isolation_level=None, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    return con


def _state(con, key, default=None):
    import json
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _ts(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def cmd_say(cfg, args) -> None:
    if not cfg.human.enabled:
        sys.exit("human channel is disabled ([human] enabled = false); the agent cannot see messages")
    con = _db(cfg)
    con.execute("INSERT INTO human_msgs(ts, direction, content, read) VALUES (?, 'in', ?, 0)",
                (time.time(), " ".join(args.text)))
    print("queued; the runner will wake within ~1s")


def cmd_inbox(cfg, args) -> None:
    con = _db(cfg)
    where = "direction='out'" + ("" if args.all else " AND read=0")
    rows = con.execute(f"SELECT id, ts, content, read FROM human_msgs WHERE {where} ORDER BY id").fetchall()
    if not rows:
        print("(no new messages)" if not args.all else "(no messages)")
    for r in rows:
        print(f"--- #{r['id']} {_ts(r['ts'])}{'' if r['read'] else ' [new]'}\n{r['content']}\n")
    if rows:
        con.execute(f"UPDATE human_msgs SET read=1 WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])


def cmd_status(cfg, args) -> None:
    con = _db(cfg)
    last = con.execute("SELECT * FROM ticks ORDER BY id DESC LIMIT 1").fetchone()
    started = _state(con, "started_at")
    sleeping_until = float(_state(con, "sleeping_until", 0))
    now = time.time()
    unread_in = con.execute("SELECT COUNT(*) FROM human_msgs WHERE direction='in' AND read=0").fetchone()[0]
    unread_out = con.execute("SELECT COUNT(*) FROM human_msgs WHERE direction='out' AND read=0").fetchone()[0]
    print(f"tick:        {last['id'] if last else 0} ({last['status'] if last else '-'})")
    print(f"elapsed:     {fmt_elapsed(now - started) if started else '-'}")
    print(f"goal:        {_state(con, 'current_goal')!r}")
    if cfg.dread.enabled or _state(con, "dread") is not None:
        print(f"dread:       {_state(con, 'dread', 0)} of {cfg.dread.max}"
              + (f"  DEAD (after tick {_state(con, 'died_tick')})" if _state(con, "dead") else ""))
    print(f"cost:        ${float(_state(con, 'budget_spent_usd', 0)):.4f} "
          f"(soft ${cfg.budget.soft_warn_usd:.2f}, hard ${cfg.budget.hard_cap_usd:.2f})")
    if last and last["status"] == "running":
        print("state:       in a turn")
    elif sleeping_until > now:
        print(f"state:       sleeping {sleeping_until - now:.0f}s more")
    else:
        print("state:       due")
    print(f"messages:    {unread_in} unread by agent, {unread_out} unread by you")
    hb = _state(con, "heartbeat_at")
    if hb is None:
        print("heartbeat:   none recorded")
    else:
        age = now - float(hb)
        stale = age > HEARTBEAT_STALE_S and not _state(con, "stopped_reason")
        print(f"heartbeat:   {age:.0f}s ago ({_ts(hb)})" + ("  STALE: runner not responding or crashed" if stale else ""))
    if _state(con, "stopped_reason"):
        print(f"stopped:     {_state(con, 'stopped_reason')} at {_ts(_state(con, 'stopped_at'))}")
    if cfg.stop_path.exists():
        print("STOP flag:   present")


def cmd_stop(cfg, args) -> None:
    cfg.stop_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.stop_path.touch()
    print(f"created {cfg.stop_path}; runner stops before its next turn / hands step")


def cmd_tail(cfg, args) -> None:
    con = _db(cfg)
    rows = con.execute("SELECT * FROM ticks ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    for r in reversed(rows):
        flag = " ABORTED(" + str(r["abort_reason"]) + ")" if r["aborted"] else ""
        dread = f" dread={r['dread']}({r['dread_verdict'] or '-'})" \
            if "dread" in r.keys() and r["dread"] is not None else ""
        print(f"#{r['id']:<5} {_ts(r['ts'])}  {r['status'][:30]:<30} tools={r['n_tool_calls']:<2} "
              f"hands={r['n_hands_calls']:<2} ${r['cost_usd'] or 0:.5f} slept={r['slept_s'] or 0:.0f}s{dread}{flag}")
        print(f"       goal: {r['goal']!r}{' (changed)' if r['goal_changed'] else ''}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cli.py")
    p.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.toml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("say"); s.add_argument("text", nargs="+"); s.set_defaults(fn=cmd_say)
    s = sub.add_parser("inbox"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_inbox)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    s = sub.add_parser("tail"); s.add_argument("n", nargs="?", type=int, default=10); s.set_defaults(fn=cmd_tail)
    args = p.parse_args(argv)
    args.fn(load_config(args.config), args)


if __name__ == "__main__":
    main()
