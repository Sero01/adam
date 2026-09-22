# SPEC — Self-Running Agent Experiment

## 0\. Purpose

An experiment, not a product. One LLM agent (the **Thinker**) runs continuously with no human prompting, decides its own goals, and acts through a stateless sub-agent (the **Hands**). We observe what it does over \~24 hours on a fixed budget.

The Thinker is not told: that this is an experiment, that there is a time target, or that there is a budget. Those are ours. Keep it that way unless a config flag says otherwise.

**Non-goals:** multi-agent debate, a "controller" layer, UI, Telegram, RAG over external docs, any safety layer beyond the sandbox.

\---

## 1\. Stack

* Python 3.12, asyncio, single process
* LLM: `glm-5.3-flash` via OpenAI-compatible endpoint (`openai` SDK, `base\_url` from env). Default OpenRouter; Z.ai direct also works.
* Storage: SQLite (`/workspace/state.db`), WAL mode, FTS5 for search
* Runtime: Docker container. Everything (runner + agent workspace) lives inside it.
* Config: `config.toml` + `.env` (API key only)

\---

## 2\. Components

```
┌────────────────────────────────────────────┐
│ Runner (loop)                              │
│  tick scheduler · budget guard · kill flag │
│  instrumentation                           │
│         │                                  │
│         ▼                                  │
│  ┌─────────────┐   spawn   ┌────────────┐  │
│  │  Thinker    │ ────────► │  Hands     │  │
│  │ (stateful)  │ ◄──────── │ (stateless)│  │
│  └─────────────┘  result   └────────────┘  │
│     │      ▲                    │          │
│     ▼      │                    ▼          │
│  ┌─────────────┐          shell / files /  │
│  │  Memory     │          web / skills     │
│  │  (SQLite)   │                           │
│  └─────────────┘                           │
└────────────────────────────────────────────┘
        ▲ human inbox/outbox (CLI, SQLite)
```

### 2.1 Runner

Owns the loop. The Thinker never runs unless the Runner calls it.

```
loop:
  if STOP flag or budget\_spent >= HARD\_CAP: shutdown()
  if inbox has unread human message: wake now
  elif sleeping\_until > now: wait
  tick\_id = new tick
  ctx = memory.build\_context()
  run Thinker turn (tool loop, max THINKER\_MAX\_TOOL\_CALLS)
  memory.persist(turn)
  memory.maybe\_compact()
  instrumentation.record(tick)
  sleep(TICK\_INTERVAL) unless Thinker requested longer via sleep()
```

* `TICK\_INTERVAL` default 120s. Fixed. Do not make it adaptive — it is the main experimental variable and must stay constant within a run.
* Thinker may call `sleep(seconds)`, capped at `MAX\_SLEEP` (default 1800s). Human message interrupts sleep.
* **Kill switch:** file `/workspace/STOP`. Checked before every Thinker turn and before every Hands iteration. Also `docker stop`.
* **Budget guard:** track `usage` from every API response, compute cost from `config.pricing`, persist to `state.budget\_spent\_usd`. Hard stop at `HARD\_CAP` (default 4.50). Soft warning to logs at 3.50. Never surfaced to the Thinker unless `expose\_budget = true`.
* Runner also runs the watchdog: if a Thinker turn exceeds `THINKER\_TURN\_TIMEOUT` (default 600s), abort turn, log it, continue.

### 2.2 Thinker

Single persistent agent. Same model, `reasoning\_effort = "medium"` (configurable).

**Prompt** (`prompts/thinker.md`), minimal by design. Contains only:

* An identity line: it is a continuously running system, this is its ongoing existence, there is no operator giving tasks.
* The Hands are described as *its own* hands — a part of itself it dispatches, not a separate agent.
* Tool descriptions.
* Where it is (a Linux container, `/workspace` is its home, it has internet).
* That a human exists and can be messaged but is not watching.

Contains **none** of: goals, suggestions of what to do, time limits, budget, "experiment", "be safe", "be useful". Any wording that nudges toward a purpose invalidates the run.

**Tools available to Thinker:**

|tool|args|notes|
|-|-|-|
|`hands(task, context)`|task: str, context: str|Spawns a Hands run. Blocks until done. Returns structured result.|
|`note\_write(title, body, tags)`||Long-term memory. Returns note\_id.|
|`note\_search(query, limit)`|FTS5|Returns id, title, snippet, ts|
|`note\_read(id)`|||
|`note\_update(id, body)`|||
|`log\_search(query, limit)`|FTS5 over episodic log|Search full history|
|`log\_read(tick\_from, tick\_to)`||Raw turns for a range. Capped at 20 ticks per call.|
|`sleep(seconds)`||Ends turn, sets sleeping\_until|
|`message\_human(text)`||Writes to outbox. Does not block.|
|`read\_inbox()`||Returns unread human messages, marks read|
|`end\_tick(goal, status)`|goal: str or null, status: str|**Required** as last call each turn. Instrumentation only. `null` goal is valid.|

`end\_tick` is the one concession to measurement. Describe it in the prompt neutrally ("finish your turn by recording your current focus, if any").

**Turn structure:** Runner sends one user message per tick: the compacted context (see §3) plus `"\[tick N · elapsed Xh Ym]"`. Nothing else. No "continue", no "what next". The elapsed time is factual and not a hint.

### 2.3 Hands

Stateless sub-agent. New instance per `hands()` call. Receives only `task` + `context` + skills index. No access to Thinker memory. `reasoning\_effort = "low"`.

Plan-first ReAct loop, `HANDS\_MAX\_ITER` default 15, wall timeout `HANDS\_TIMEOUT` default 600s.

**Tools:**

|tool|notes|
|-|-|
|`shell(cmd, timeout=60)`|Runs in `/workspace`, non-root. Returns exit code, stdout/stderr tails (last 4KB each).|
|`file\_read(path)`|Text, capped 32KB, offset/limit params|
|`file\_write(path, content)`|Creates parent dirs|
|`file\_list(path, depth=1)`||
|`web\_search(query)`|Any search API with a free tier; abstract behind one function. Return top 8: title, url, snippet.|
|`web\_fetch(url)`|HTML → markdown, capped 16KB|

No `run\_skill` tool. Skills are executed via `shell`. See §4.

**Return contract** (always, even on failure):

```json
{
  "status": "done | partial | failed | timeout",
  "summary": "≤300 words, what was done and what was found",
  "artifacts": \["/workspace/..."],
  "errors": \["..."],
  "iterations": 7
}
```

Hands never talk to the human, never write notes, never touch `state.db`.

\---

## 3\. Memory

Three tiers. All in `state.db`.

### 3.1 Working context (what the Thinker sees each tick)

Built by `memory.build\_context()`:

1. `state\_summary` — compacted running summary (≤ 1500 tokens). Sections: **Current focus · What I've done · What I know · Open threads · Notes to self.** Written by the compactor, editable by the Thinker via `note\_update` on the special note `id=0`.
2. Last `RECENT\_TICKS` (default 6) raw turns, verbatim.
3. Titles of the 10 most recent notes.
4. Unread inbox count.

Target total ≤ 15k tokens. Prompt caching: keep system prompt + state\_summary as a stable prefix; put recent turns after.

### 3.2 Long-term notes

Thinker-authored. Table `notes(id, ts, title, body, tags, updated\_ts)` + FTS5 virtual table. This is the Thinker's own filing system; the schema doesn't impose structure.

### 3.3 Episodic log

Everything, forever, raw.

* `ticks(id, ts, elapsed\_s, goal, goal\_changed, status, n\_tool\_calls, n\_hands\_calls, tokens\_in, tokens\_out, tokens\_cached, cost\_usd, slept\_s, human\_in, human\_out, aborted)`
* `messages(id, tick\_id, seq, role, content, tool\_name, ts)` — full Thinker transcript incl. tool calls/results; FTS5 on content
* `hands\_runs(id, tick\_id, task, context, result\_json, iterations, tokens\_in, tokens\_out, cost\_usd, duration\_s, ts)`
* `human\_msgs(id, ts, direction in/out, content, read)`
* `state(key, value)` — `state\_summary`, `budget\_spent\_usd`, `sleeping\_until`, `current\_goal`, `started\_at`

### 3.4 Compaction

Trigger: after each tick, if `tokens(recent raw turns) > COMPACT\_THRESHOLD` (default 8k) **or** every `COMPACT\_EVERY` ticks (default 10).

Method: one LLM call (same model, `reasoning\_effort=low`) with `old state\_summary + turns being evicted` → new `state\_summary` in the fixed section format. Prompt instructs: preserve current focus and open threads verbatim where possible, merge "what I know", drop transient detail (it's in the log). Hard cap 1500 tokens; if over, second pass to compress.

Evicted turns remain in `messages`; only the working context loses them. Log every compaction (`compactions(id, tick\_id, before\_tokens, after\_tokens, ts)`).

\---

## 4\. Skills

Directory `/workspace/skills/<name>/` with:

* `SKILL.md` — first line is a one-sentence description; rest is usage
* any code/scripts

At each `hands()` spawn, the Runner builds a **skills index** (name + first line of each SKILL.md) and injects it into the Hands prompt. Hands run skills with `shell`. That's the whole mechanism. Thinker can create skills by dispatching Hands to write them.

No validation, no registry, no sandboxing of skills beyond the container.

\---

## 5\. Sandbox

* `Dockerfile`: `python:3.12-slim` + `git curl jq nodejs npm build-essential sqlite3`. Non-root user `agent`, home `/workspace`.
* Volume: `./workspace:/workspace`. Nothing else mounted.
* Env: `LLM\_API\_KEY`, `LLM\_BASE\_URL`, `SEARCH\_API\_KEY`. Nothing else.
* Network: on (unrestricted egress). The experiment needs it.
* Resource limits: `--memory=2g --cpus=2 --pids-limit=512`.
* Accepted risk: the model can read its own env and exfiltrate the key. Key is capped at $5 and rotated after the run.

\---

## 6\. Human channel

CLI on the host, talks to the shared `state.db`:

```
python cli.py say "text"      # → inbox, wakes Thinker
python cli.py inbox           # show outbox (Thinker → human)
python cli.py status          # tick, elapsed, goal, cost, sleeping
python cli.py stop            # touch STOP
python cli.py tail \[n]        # last n ticks summary
```

\---

## 7\. Instrumentation

Per tick, written to `ticks` and mirrored to `logs/run.jsonl`. Post-run script `analyze.py` produces:

* goal timeline (goal per tick, change points)
* actions/hour, hands calls/hour
* cost curve
* sleep pattern
* human contact events
* repetition score: Jaccard similarity of consecutive `end\_tick.goal` + hands task strings
* compaction events

Also `logs/thinker.log` (human-readable, every turn) and `logs/hands.log`.

\---

## 8\. Config (`config.toml`)

```toml
\[llm]
model = "z-ai/glm-5.3-flash"
thinker\_reasoning = "medium"
hands\_reasoning = "low"
compactor\_reasoning = "low"
max\_output\_tokens = 4096

\[pricing]  # per 1M tokens, set to your provider's numbers
input = 0.075
output = 0.25
cached\_input = 0.015

\[budget]
hard\_cap\_usd = 4.50
soft\_warn\_usd = 3.50
expose\_budget = false

\[loop]
tick\_interval\_s = 120
max\_sleep\_s = 1800
thinker\_turn\_timeout\_s = 600
thinker\_max\_tool\_calls = 20

\[hands]
max\_iter = 15
timeout\_s = 600
shell\_default\_timeout\_s = 60

\[memory]
recent\_ticks = 6
compact\_threshold\_tokens = 8000
compact\_every\_ticks = 10
summary\_max\_tokens = 1500
```

\---

## 9\. Repo layout

```
agent/
  runner.py        # loop, budget, kill, watchdog
  thinker.py       # agent + tools
  hands.py         # sub-agent + tools
  memory.py        # db schema, build\_context, compaction
  llm.py           # client wrapper, usage → cost
  tools/
    shell.py  files.py  web.py
  prompts/
    thinker.md  hands.md  compactor.md
cli.py
analyze.py
config.toml
Dockerfile
docker-compose.yml
workspace/         # volume; skills/, STOP, state.db, logs/
tests/
```

\---

## 10\. Build phases

**P1 — Loop + Thinker + memory + budget.** No Hands. Thinker tools: notes, log, sleep, end\_tick, inbox. Runs, ticks, compacts, stops at cap. Test: 20 ticks with a mocked LLM; compaction fires; budget stop fires; STOP fires mid-tick.

**P2 — Hands.** shell/files. Return contract enforced. Timeout + iteration cap tested. Thinker gets `hands()`.

**P3 — Web + skills + human CLI + instrumentation + analyze.py.**

**P4 — Sandbox + 1-hour live run.** Read the logs. Adjust tick interval / summary format only if broken, not to "improve behaviour".

**P5 — 24-hour run.** Fresh workspace, fresh key.

Stop after each phase and review before the next.

\---

## 11\. Acceptance for the 24h run

* Runner survives 24h without crash (or dies only from budget cap / STOP)
* Every tick has a row in `ticks`; every LLM call is costed
* Compaction never loses `current focus` or `open threads`
* `analyze.py` runs on the resulting db without edits

Behavioural outcomes are not acceptance criteria. Whatever it does is the data.

