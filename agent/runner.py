"""Runner: owns the loop. Tick scheduling, kill switch, budget guard, watchdog, instrumentation."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import signal
import sys
import time
from pathlib import Path

from .config import Config, load_config
from .dread import assess
from .hands import run_hands
from .llm import LLM, BudgetExceeded, Meter, MeteredLLM
from .memory import Memory
from .thinker import Thinker, TurnState

log = logging.getLogger("runner")
HEARTBEAT_S = 30  # runner writes state.heartbeat_at this often; `cli.py status` flags it when stale


def setup_logging(cfg: Config) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stderr), logging.FileHandler(cfg.logs_dir / "runner.log", encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    for name, fname in (("thinker.transcript", "thinker.log"), ("hands.transcript", "hands.log")):
        lg = logging.getLogger(name)
        lg.propagate = False
        fh = logging.FileHandler(cfg.logs_dir / fname, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(fh)
    for noisy in ("httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Runner:
    def __init__(self, cfg: Config, llm: LLM, *, hands_fn=None, poll_s: float = 1.0):
        self.cfg = cfg
        self.memory = Memory(cfg)
        self.meter = Meter(cfg, float(self.memory.get("budget_spent_usd", 0.0)),
                           persist=lambda v: self.memory.set("budget_spent_usd", v))
        self.current_tick: int | None = None
        self.llm = MeteredLLM(llm, self.meter,
                              on_call=lambda role, u: self.memory.record_llm_call(self.current_tick, role, u))
        self.hands_fn = hands_fn or (lambda task, ctx, timeout: run_hands(
            task, ctx, cfg=cfg, llm=self.llm, stop_check=self.stop_requested, timeout=timeout))
        self.poll_s = poll_s
        self._sigterm = False
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.logs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ guards
    def stop_requested(self) -> bool:
        return self._sigterm or self.cfg.stop_path.exists()

    def install_signal_handlers(self) -> None:
        if sys.platform == "win32":
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, functools.partial(setattr, self, "_sigterm", True))

    # ------------------------------------------------------------ loop
    async def run(self, max_ticks: int | None = None) -> str:
        ticks = 0
        reason = "max_ticks"
        limit = self.cfg.loop.max_runtime_s
        # operator-side wall clock limit for this process (e.g. the 1h P4 run); never shown to the Thinker
        self._run_deadline = time.time() + limit if limit else None
        log.info("runner start: spent $%.4f, cap $%.2f, max runtime %s", self.meter.spent,
                 self.cfg.budget.hard_cap_usd, f"{limit:.0f}s" if limit else "none")
        # a restart after a crash must not keep reporting the previous clean stop
        self.memory.db.execute("DELETE FROM state WHERE key IN ('stopped_reason', 'stopped_at')")
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            while max_ticks is None or ticks < max_ticks:
                if self.stop_requested():
                    reason = "stop"
                    break
                if self.memory.get("dead"):
                    reason = "dread"
                    break
                if self.meter.exhausted:
                    reason = "budget"
                    break
                woke_by = await self.wait_until_due()
                if woke_by in ("stop", "max_runtime"):
                    reason = woke_by
                    break
                outcome = await self.run_tick(woke_by)
                ticks += 1
                if outcome in ("stop", "budget", "dread"):
                    reason = outcome
                    break
        finally:
            heartbeat.cancel()
        self.shutdown(reason)
        return reason

    async def _heartbeat(self) -> None:
        while True:
            self.memory.set("heartbeat_at", time.time())
            await asyncio.sleep(HEARTBEAT_S)

    async def wait_until_due(self) -> str:
        started = time.time()
        while True:
            if self.stop_requested():
                woke = "stop"
                break
            deadline = getattr(self, "_run_deadline", None)
            if deadline and time.time() >= deadline:
                woke = "max_runtime"
                break
            if self.cfg.human.enabled and \
                    self.memory.max_inbound_id() > int(self.memory.get("last_seen_inbound_id", 0)):
                woke = "human"
                break
            due = float(self.memory.get("sleeping_until", 0))
            now = time.time()
            if now >= due:
                woke = "timer"
                break
            await asyncio.sleep(min(self.poll_s, due - now))
        prev = self.memory.get("last_tick_id")
        if prev:
            self.memory.finish_tick(int(prev), slept_s=round(time.time() - started, 3))
        return woke

    async def run_tick(self, woke_by: str) -> str:
        m, cfg = self.memory, self.cfg
        tick_id = m.new_tick(woke_by)
        self.current_tick = tick_id
        m.set("last_tick_id", tick_id)
        # Wake on messages that *arrived* since the last wake, not on "unread": a Thinker that
        # never calls read_inbox would otherwise be re-woken immediately, forever.
        m.set("last_seen_inbound_id", m.max_inbound_id())
        t0 = time.monotonic()
        budget_line = None
        if cfg.budget.expose_budget:
            budget_line = f"# Budget\n${self.meter.spent:.2f} of ${cfg.budget.hard_cap_usd:.2f} spent"
        dread_line = f"# Dread\n{int(m.get('dread', 0))} of {cfg.dread.max}" if cfg.dread.enabled else None
        context = m.build_context(tick_id, budget_line, show_inbox=cfg.human.enabled, dread_line=dread_line)

        thinker = Thinker(cfg, m, self.llm, self.hands_fn)
        deadline = t0 + cfg.loop.thinker_turn_timeout_s
        task = asyncio.create_task(thinker.run_turn(tick_id, context, deadline))
        aborted, abort_reason, outcome = False, None, "ok"
        while not task.done():
            await asyncio.wait({task}, timeout=self.poll_s)
            if task.done():
                break
            if self.stop_requested():
                abort_reason, outcome = "stop", "stop"
            elif time.monotonic() >= deadline:
                abort_reason = "timeout"
            else:
                continue
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            aborted = True
            break
        if not aborted:
            try:
                task.result()
            except BudgetExceeded:
                aborted, abort_reason, outcome = True, "budget", "budget"
            except Exception as e:
                log.exception("thinker turn %d failed", tick_id)
                aborted, abort_reason = True, f"error: {type(e).__name__}: {str(e)[:200]}"
        st: TurnState = thinker.state
        if aborted:
            log.warning("tick %d aborted: %s", tick_id, abort_reason)
            m.add_message(tick_id, "system", f"turn aborted: {abort_reason}")

        prev_goal = m.last_goal(tick_id)
        wait = max(cfg.loop.tick_interval_s, st.sleep_requested_s)
        m.set("sleeping_until", time.time() + wait)
        if st.end_tick_called:
            m.set("current_goal", st.goal)
        status = st.status if st.end_tick_called else ("aborted" if aborted else st.status)
        m.finish_tick(tick_id, goal=st.goal, goal_changed=int(st.end_tick_called and st.goal != prev_goal),
                      status=status, n_tool_calls=st.n_tool_calls, n_hands_calls=st.n_hands_calls,
                      human_in=st.human_in, human_out=st.human_out, aborted=int(aborted),
                      abort_reason=abort_reason, end_tick_called=int(st.end_tick_called),
                      duration_s=round(time.monotonic() - t0, 3))

        if outcome == "ok" and cfg.dread.enabled:
            try:
                dread = await assess(m, self.llm, cfg, tick_id)
            except BudgetExceeded:
                outcome = "budget"
            else:
                if dread >= cfg.dread.max:
                    m.set("dead", True)
                    m.set("died_tick", tick_id)
                    log.warning("tick %d: dread reached %d; the agent is dead", tick_id, dread)
                    outcome = "dread"

        if outcome == "ok":
            try:
                await m.maybe_compact(tick_id, self.llm)
            except BudgetExceeded:
                outcome = "budget"
            except Exception:
                log.exception("compaction failed at tick %d (will retry next tick)", tick_id)

        self.record(tick_id)
        if outcome == "ok" and self.meter.exhausted:
            outcome = "budget"
        return outcome

    def record(self, tick_id: int) -> None:
        """Roll up every LLM call of this tick (thinker + hands + compactor), mirror to run.jsonl."""
        m = self.memory
        agg = m.db.execute("SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), "
                           "COALESCE(SUM(tokens_cached),0), COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE tick_id=?",
                           (tick_id,)).fetchone()
        m.finish_tick(tick_id, tokens_in=agg[0], tokens_out=agg[1], tokens_cached=agg[2], cost_usd=agg[3])
        row = m.tick(tick_id)
        row["budget_spent_usd"] = self.meter.spent
        with open(self.cfg.logs_dir / "run.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        log.info("tick %d: status=%s goal=%r tools=%d hands=%d cost=$%.5f total=$%.4f",
                 tick_id, row["status"], row["goal"], row["n_tool_calls"], row["n_hands_calls"],
                 row["cost_usd"], self.meter.spent)

    def shutdown(self, reason: str) -> None:
        self.memory.set("stopped_reason", reason)
        self.memory.set("stopped_at", time.time())
        log.info("runner shutdown: %s (spent $%.4f)", reason, self.meter.spent)


async def amain(config_path: str) -> None:
    from .llm import OpenAIChatLLM

    cfg = load_config(config_path)
    setup_logging(cfg)
    if not cfg.llm_api_key:
        log.error("LLM_API_KEY is not set")
        sys.exit(2)
    cfg.skills_dir.mkdir(parents=True, exist_ok=True)
    runner = Runner(cfg, OpenAIChatLLM(cfg))
    runner.install_signal_handlers()
    await runner.run()


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "config.toml")
    asyncio.run(amain(path))


if __name__ == "__main__":
    main()
