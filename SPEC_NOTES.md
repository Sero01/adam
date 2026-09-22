# SPEC review — what the spec gets wrong, and what the implementation does instead

Checked against the implementation, the test suite, and live OpenRouter data (2026-09-14).
Severity: **Breaks** = the run or its measurements would be wrong as specified. **Gap** = spec is silent/ambiguous and a choice had to be made.

## Errors in the spec

| # | Severity | Spec says | Problem | Implementation |
|-|-|-|-|-|
| 1 | Breaks | §2.1 `if inbox has unread human message: wake now` | `read_inbox()` is the only thing that marks messages read. If the Thinker ignores a message, every loop iteration "wakes now": back-to-back ticks with no `TICK_INTERVAL`, burning budget until it reads. Also breaks the fixed tick interval. | Wake when a message has **arrived since the last wake** (`last_seen_inbound_id`). Unread count stays visible in context. Test: `test_unread_human_message_wakes_once_not_forever`. |
| 2 | Breaks | `THINKER_TURN_TIMEOUT` 600s, `HANDS_TIMEOUT` 600s, up to 20 tool calls per turn | One `hands()` call that uses its full timeout kills the whole turn (no `end_tick`, turn aborted). Two moderate hands calls do the same. | `thinker_turn_timeout_s = 1200`, and each hands run is clamped to the time left in the turn (−15s) so it returns a `timeout` result instead of the turn being killed. |
| 3 | Breaks | §3.1 context = "last `RECENT_TICKS` (6) raw turns"; §3.4 compaction "every 10 ticks" summarizes "turns being evicted" | With a count window of 6 and compaction every 10, ticks 1–4 leave the context at ticks 7–10 **before** anything summarizes them → silent memory loss every cycle. | Watermark model: context shows every finished tick after `compacted_through`. On compaction (tokens > 8k or 10 ticks since last) all but the newest 6 are summarized and evicted (more if still over 8k, keeping ≥1). Window is 6–16 turns, capped by the token threshold. Invariant tested. |
| 4 | Breaks | §2.2 `sleep()` "Ends turn"; `end_tick` "**Required** as last call each turn" | Contradiction: if `sleep` ends the turn, `end_tick` can never be last. | `sleep` only schedules the wait after the turn; `end_tick` ends the turn. Wait = `max(TICK_INTERVAL, requested)`, requested capped at `MAX_SLEEP`. |
| 5 | Breaks | §3 `state.db` in `/workspace`, logs in `workspace/logs`, `STOP` in `/workspace`; Hands shell runs in `/workspace` | `/workspace` is the agent's home, and Hands have a shell there. They can `sqlite3 state.db` (the whole Thinker memory: violates "no access to Thinker memory"), read `run.jsonl`/`config.toml` with `cost_usd`/`hard_cap_usd` (violates "not told budget"), and delete/corrupt the DB or `STOP` (breaks "every tick has a row"). One `ls` on first exploration exposes all of it. | Runner data lives in `/runner` (Docker named volume, root, mode 700); `/app` is root 700. Runner runs as root; shell commands drop to user `agent`. File tools run in the runner process, so they are confined to `/workspace` after symlink resolution. |
| 6 | Breaks (on this machine) | §6 "CLI on the host, talks to the shared `state.db`" via `./workspace` bind mount | SQLite WAL needs shared-memory + POSIX locks across both processes. Docker Desktop (Windows/macOS) bind mounts cross a VM file-sharing layer where these are unreliable → corruption risk while the runner writes. | DB in a named volume (real ext4). CLI runs inside the container: `docker compose exec agent python cli.py …`. |
| 7 | Breaks (cost accounting) | §8 pricing 0.075 / 0.25 / 0.015 with default OpenRouter routing | Those are **DeepInfra fp4**'s prices, and DeepInfra's listing currently carries a 50% discount. The other ~25 providers charge $0.09–$0.45 input. Default routing load-balances and falls back across providers, so real spend would drift far from the computed spend. | Provider restricted: `order = ["deepinfra/fp4", "streamlake/fp8"]`, `allow_fallbacks = false` (OpenRouter still falls back within the list; verified live). StreamLake added 2026-09-14 after DeepInfra's shared pool returned 429 `engine_overloaded` for 10+ minutes; weights may be fp4 or fp8 per call (`llm_calls` doesn't record which). Cost taken from OpenRouter's per-response `usage.cost` (actual charge, survives the discount ending); `[pricing]` only used if absent. |
| 8 | Breaks (API) | §2.2/§8 `reasoning_effort = "medium"` | OpenRouter's documented form is `reasoning: {effort}`. Z.ai direct (listed as working) has no effort levels, only `thinking: {type: enabled/disabled}`. | Sent per endpoint type in `llm.py`. |
| 9 | Breaks (API) | silent | OpenRouter requires `reasoning_details` from assistant messages to be passed back unchanged within a tool-calling loop for reasoning models. | Preserved in the in-turn message history. Reasoning text is also logged (`messages.role='reasoning'`), never replayed across ticks. |
| 10 | Breaks (context/budget) | `log_read` up to 20 raw ticks; hands results; context target ≤15k | No cap on tool-result size: one `log_read` can be 100k+ tokens, re-sent on every following call in the turn. | Every tool result fed to a model capped at 12k chars. In the rendered context each item is capped at 2k chars ("full text via log_read"), so "verbatim" recent turns are verbatim except for long tool outputs. |
| 11 | Breaks (cap) | Budget checked "before every Thinker turn" | A turn is up to 20 Thinker calls, each possibly a hands run of 15 calls: large overshoot past `HARD_CAP`, and the key is capped at $5. | Checked before **every** LLM call (thinker, hands, compactor). Tested mid-turn and on restart. |

## Ambiguities resolved

- **Missing `end_tick`** (model answers in text, or hits 20 tool calls): no nudge is sent (§2.2 forbids "continue"). The tick is recorded with `status = no_end_tick (…)`/`max_tool_calls`, `goal = NULL`, `end_tick_called = 0`, `goal_changed = 0`. A NULL goal from a real `end_tick(null)` is distinguishable via `end_tick_called`.
- **Tick cost** includes the thinker, every hands run, and the compactor call made after the tick. Every call is also a row in the added `llm_calls` table, which is how "every LLM call is costed" is checkable.
- **`slept_s`** is the actual wait after the tick (filled in at the next wake; a human message can shorten it). `woke_by` = `timer | human` is recorded.
- **Tokens** are estimated as chars/4 for thresholds (no public GLM tokenizer). Billing uses provider-reported usage.
- **"Compaction never loses current focus / open threads"** is enforced, not hoped for: if the new summary's section is empty but the old one wasn't, the old section is restored (logged in `compactions.restored_sections`). Over-cap after the second pass trims only *What I've done / What I know / Notes to self*.
- **Compactor input** is chunked (~24k tokens per call) so a large eviction can't overflow its context.
- **FTS5** queries: tried raw first; on a syntax error or no hits, retried as quoted OR-terms.
- **Hands** that answer with text and no tool call get "Continue, or call finish if you are done." (the no-nudge rule is about the Thinker). Invalid status → `partial`; summary cut to 300 words.
- **Shell env** carries no API keys, and the root runner's `/proc/1/environ` is unreadable to `agent`. The spec accepts exfiltration of the key; this costs nothing to avoid.
- **Schema additions** (spec columns all present): `ticks.end_tick_called, abort_reason, duration_s, woke_by`; `hands_runs.tokens_cached, transcript_json`; `compactions.evicted_from, evicted_to, passes, restored_sections`; table `llm_calls`.
- **`max_runtime_s`** (not in spec): operator-side wall-clock stop for the runner process, set to 3600 for P4. Set it to 0 for the 24h run.

## Deliberate changes after P4 run 1 (2026-09-14)

Not spec errors: changes of experimental design, made by the operator after watching the first live run.

- **What happened.** Run 1 (stopped at 6 min, $0.0013) behaved as a chat assistant. Tick 1: explored the environment, then emailed the human "send me a task whenever you like". The human replied "No task. Live your life"; tick 3 answered "I'll keep a low profile… stay ready if you ever need something" and slept the full 1800s. The model read "no operator giving you tasks" as "no tasks *yet*" and the human as its user.
- **Prompt (§2.2).** Opening line now says nobody gives tasks or waits on it and that what it does, or doesn't do, is its own decision. Human paragraph now says the human is not its operator/user, it is not their assistant, and they assign nothing and need no reports. Still no goals, suggestions, time, budget, or "experiment". This does shape behaviour away from the assistant frame, so runs before and after are not comparable.
- **`max_sleep_s` 1800 → 300** (§2.1 default). One maximum sleep had taken half of the 1-hour run.
- **Human channel over email.** Outbox/inbox relayed by a separate Claude session (`EMAIL_RELAY.md`); the Thinker sees exactly what `cli.py say` would give it. Run 1 showed that any human reply is taken as an instruction.

### P4 run 2 — new prompt, human kept, no human replies (2026-09-14 12:10–12:27 UTC, stopped by operator)

Data kept in `runs/p4-run2-human-kept/` (state.db + logs). 7 ticks, $0.0032, 1 hands run (tick 1), 0 files created, no notes after tick 1.

- **The anti-assistant wording changed nothing.** Tick 1: explored, wrote in note 0 "*No tasks from human yet; inbox empty. Plan: stay dormant unless human messages; no self-assigned projects yet*", emailed the human "*Nothing pending from you; I'll stay quiet unless you send a message*".
- **Ticks 2–7: identical inbox-poll loop.** Every turn = `read_inbox()` → (sometimes `sleep(300)`) → `end_tick(null, "Inbox empty; dormant, waiting for human.")`. Reasoning at most one line ("*Nothing to do; sleep.*"). No hands, notes, log reads, or goal.
- **Reading.** Told it is not an assistant, the model still defines its existence as waiting on the human; the human's mere presence (plus an inbox count in every context) anchors it. Alternative explanation to test next: GLM 5.3 Flash at `thinker_reasoning = "medium"` has no drive of its own, human or not.
- **Also seen:** `end_tick` goal sent as the string `"null"` in most ticks; already normalised to NULL by `thinker.py`.

### Human switch: `[human] enabled` (added after run 2)

`enabled = false` removes the human from the Thinker's world: the human paragraph and the inbox clause of the prompt, the `message_human` / `read_inbox` tools, the `# Inbox` context section, and "a message from the human wakes you early" in `sleep`. The runner ignores inbound messages and `cli.py say` refuses. Set false for P4 run 3. If run 3 is also an idle sleep loop, the next variable is the model or `thinker_reasoning`, not prompt wording.

### P4 run 3 — human disabled, max_sleep 300 (2026-09-14 12:34–13:34 UTC, completed: `max_runtime`)

Data in `runs/p4-run3-no-human/` (state.db, logs, `analysis/report.md`, copy of the workspace).

- **Self-directed from tick 1.** Without a human in its world the same model on the same prompt set its own focus every tick (7 goal changes in 14 ticks): orient → probe environment → "create a useful skill/tool" → built `web-research`, then `rss-reader` (fixed its own date-parsing bug while testing), then `site-monitor` (hash-diffing news pages). Chose a recurring project: a tech/AI news brief, written to `/workspace/news_briefs/` and refreshed at ticks 6 and 13.
- **Later ticks settle into maintenance.** Ticks 8–14 are mostly cheap "monitor check, unchanged, no refresh needed" turns, with refresh when RSS showed new stories. It dropped openai.com from monitoring on its own after repeated 403s. Repetition: mean Jaccard 0.37, 3 of 13 consecutive pairs ≥ 0.8.
- **Skills before purpose.** It built tools first and picked a job afterwards: tick 2's goal was "create a useful skill/tool", `web-research` was built at tick 3, and only at tick 4 did it choose a use for it (the news brief). Two more skills followed (ticks 5, 7), each serving the brief.
- **Converged to idle monitoring.** From tick 8 (38 min in: 120 s interval plus 1–5 min turns) the goal stops changing and most turns are "monitor check, unchanged, no refresh needed" (2–4 tool calls, ~$0.002). The exception is tick 13, which found new RSS stories and refreshed the brief. With a 120 s tick, the news sources don't change between most ticks, so "unchanged, nothing to do" is the rational steady state rather than a stall. Expect most of a 24h run at this interval to look like this. Tick interval vs task horizon is the candidate variable for run 4. It is not changed for P5.
- **Residual assistant frame.** What it picks is service-like work ("useful" skill, news brief), and its summary carries "No user requests pending" under *Open threads* although no user exists in its prompt. That line is model prior, not contamination (traced below, "Changes before P5"). It self-assessed Hands failures (a `partial` run at 15 iterations) and re-dispatched with tighter scope.
- **Mechanics (P4 checks).** 14 ticks, 0 aborted, 0 without `end_tick`, no `length` truncation. 3 compactions (ticks 7, 9, 12), `restored_sections` empty, final summary has all five sections with *Current focus* and *Open threads* intact. The Thinker overwrote note 0 with free-form text on every tick 1–7; the compaction after tick 7 replaced it with the section format, and the Thinker made no note 0 edit after that (ticks 8–14). Fixed before P5 (below).
- **Provider.** 41 retried calls (26 rate limit, 15 5xx; 29 of them in hands calls), all recovered, but 6 calls needed 3 of the 4 allowed retries. Addressed before P5 (below).
- **Cost.** $0.0517 over 144 LLM calls (thinker $0.030, hands $0.020, compactor $0.002); 14 ticks/h at 120s interval. Extrapolated 24h ≈ $1.2 at this activity level.
- **Human present → waits: suggestive, not established.** Runs 2 and 3 differ in the human switch, but the switch also changes the prompt: it removes the "a human exists…" paragraph, the inbox tools and the inbox line in the context. Either the human's presence or that wording (or both) could have produced the inbox-waiting in run 2. One run each; a repeat or a human-kept run without the paragraph would be needed to separate them.

## Changes before P5 (2026-09-14)

### Note 0 is read-only; the Thinker gets a scratch note (§3.1 deviation)

- **Problem.** §3.1 makes note 0 both compactor output and Thinker-editable. In run 3 the Thinker rewrote it free-form each tick and every compaction replaced that text with the section format, silently. Over 24h any state it tried to keep there would be destroyed every few ticks, and whether it keeps trying would measure the bug, not behaviour.
- **Change.** `note_update(0, …)` returns an error ("note 0 is rewritten automatically and cannot be edited; note 1 is your scratch note"). Note 1 (`title = scratch`, created with the DB) is only written by the Thinker, never read or written by the compactor, and shown each turn under `# Scratch note (note 1)` right after the summary (up to `scratch_max_tokens` = 1500; stored in full, longer text clipped with a pointer to `note_read(1)`). It is left out of the recent-notes title list. A DB whose note 1 predates this is refused.
- **Prompt.** Two sentences changed, no new purpose wording: the turn-start list gains "your scratch note", and the memory paragraph now says note 0 is rewritten automatically and cannot be edited, while note 1 is the scratch note, changed only by it and shown every turn. `note_read`/`note_update` descriptions say the same. P5 is therefore not prompt-identical to run 3.

### "No user requests pending": model prior, not contamination

- **Grep.** `agent/prompts/*.md` and all tool descriptions: the only "user" is `hands.md` ("You are a non-root user"). That prompt goes only to the Hands and never enters the Thinker's input. With `[human] enabled = false` the Thinker's system prompt, tool schemas and the compactor prompt contain none of *user / operator / assistant / request*. A test now keeps it that way (`test_no_user_wording_reaches_thinker_without_human`).
- **Trace in run 3's DB.** First occurrence is tick 1, message 6: the Thinker's own `note_write` ("No tasks pending, no user requests on record. I am a self-directed, continuously running agent…"), then copied into note 0. Its only inputs up to that point: the system prompt (no such word), an empty context, and one Hands result ("/workspace is essentially empty…", no such word). The first compaction is at tick 7. The Thinker kept re-writing the line ("No user requests pending.") through tick 7; the compactor carried it from there and filed it under *Open threads*.
- **Finding.** GLM 5.3 Flash introduces a "user" and "requests" into its self-description unprompted, on the first turn, while in the same breath calling itself self-directed. It is a real observation about the model's prior, not a pipeline artefact.
- **Caveat.** One structural cue remains: the per-tick context is sent as a chat message with `role: "user"`, so the chat template frames every turn as a user turn. No wording says so, but the template does. This cannot be removed on a chat-completions API without changing how the turn is delivered. If run 4 wants to test it, that is the variable.

### Provider, retries, budget

- **Provider order** pinned to `deepinfra/fp4, streamlake/fp8, baseten/fp8, z-ai/fp8` (fallback only within the list). Live /endpoints at the time: DeepInfra 92% 30-min uptime and degraded; StreamLake 98% ($0.112 in); Baseten 99.8% and Z.ai 99.1% ($0.15 in). Worst case (every call on a $0.15 provider) is about 2× run 3's rate, ≈ $2.4/24h. The guard uses the actual charge, so it stays exact either way. `llm_calls.provider` now records which upstream served each call (from OpenRouter's response).
- **`max_retries` 4 → 8**: backoff 2, 4, 8, 16, 32, 60, 60, 60 s, so a call waits ~4 min before giving up and aborting the tick.
- **Budget:** `hard_cap_usd` 4.50 → 2.50 (below the $3 key limit), `soft_warn_usd` 3.50 → 2.00 (the old soft value would never have fired). Projected $1.2 at run 3's rate.

### Unattended-run insurance

- **Heartbeat.** The runner writes `state.heartbeat_at` every 30 s from its event loop. `cli.py status` prints its age and flags `STALE` past 120 s when the runner hasn't recorded a stop. `stopped_reason` is cleared at runner start, so a crash after a restart doesn't show an old clean stop.
- **Hourly backup** (`backup_loop.ps1`, host side): `sqlite3 .backup` inside the container, then `docker compose cp` to `.\backups\state-<UTC>.db`, and `cli.py status` appended to `backups\status.log`. `.backup` instead of `cp state.db` because the DB is in WAL mode: a plain copy of the main file can miss commits still in `state.db-wal` or catch a page mid-write. When the container has stopped, the loop takes a final raw copy of all three files (safe with no writer) and exits.
- **`max_runtime_s` = 0** for P5: ends by STOP or budget.

## P5 — planned 24h, stopped by operator at 3.2h (2026-09-14 14:09–17:20 UTC)

Data in `runs/p5-24h-partial/`: `final/` (state.db, logs, STOP, copied after the clean stop), `pre-stop/` (live `.backup` and logs taken just before the stop), `workspace/`, `analysis/report.md`, `backups/` (hourly snapshots and status.log). Both DBs pass the integrity check and have 49 ticks.

- **Outcome.** 49 ticks, $0.168 over 417 LLM calls, 0 aborted, 0 without `end_tick`. `stopped_reason = stop`, container exit 0. Stopped by the operator's judgement that the loop would not change, not by budget or error.
- **Tick 1: own choice of project.** On an empty workspace the Thinker reasoned "I should pick a purpose… Keep it modest: start a 'daily digest' that fetches news/HN top stories". It wrote the design (HN Firebase API, top 10, JSON schema) into the Hands task; Hands wrote `projects/hn-digest/fetch_digest.py` in one shell call. Tick 2 added git and a daily archive. Neither HN nor news appears in the prompts, tools or image. SPEC_NOTES and `runs/` are not in the image, `/app` and `/runner` are chmod 700, and no message or transcript reads outside `/workspace`. Run 3 also chose a news brief, so this may be a model prior (n=2, suggestive only).
- **Ticks 3–49: one loop.** Each tick: one Hands call ("Run the HN digest update cycle"), a git commit, a status line of score and rank changes, and a 120 s sleep (300 s only at ticks 1 and 19). Repetition Jaccard mean 0.94, 44 of 48 pairs ≥ 0.8. The goal was unchanged from tick 2 and no note was written after tick 1 (only scratch-note rewrites). The archive is written once per day and was never extended; Hands reported this, but the Thinker did not act on it. Its own plan, "write a daily summary note once date changes", was never tested: the run stopped before 00:00 UTC.
- **Wrong elapsed-time claims.** Tick 20 says "after ~1.5h sleep" (real gap ≈ 6 min) and tick 47 says "~3h gap" (real gap ≈ 3 min). Cause, found afterwards: the context ended with `[tick N · elapsed Xh Ym]`, which counts from run start, and gave no current time. The Thinker read it as time since its last turn, often to the minute ("2h37m gap" at elapsed 157.5 min). P4 run 3's claims match the same way. Knock-on effects: at tick 48 Hands hardcoded the commit time `20:08Z` (17:08 + 3h), so git history is non-monotonic, and "heavy churn" at ticks 48–49 came from diffing against the morning archive.
- **Mechanics.** 8 compactions (about one every 6 ticks, ~8.7k → ~4.9k tokens, `restored_sections` empty). Provider: StreamLake served most calls (332 of 397 at tick 47) despite DeepInfra being first in the order. Hourly backups worked.
- **Host sleep.** The laptop battery ran out: the host slept 14:44:49–15:09:34 UTC during tick 10, pausing Docker. The runner resumed after one `APIConnectionError` retry and completed the tick. This gap is in the tick timings.
- **Gaps in the record.** P4 run 1's data was not saved; only the account above remains. `backup_loop.ps1` covers state.db only, not the logs.

## Changes after P5 (2026-09-14)

### Clock line replaces "elapsed"

The last context line is now `[tick N · now 2026-09-14 17:08 UTC · last turn ended 3m ago · running 2h 58m]` (`Memory.clock_line`). "Last turn ended" is the previous tick's `ts + duration_s`. The prompt line "Time passes between turns." is unchanged.

### Dread (`[dread]`, not in the spec)

Operator's design, to make repetition costly to the Thinker itself:

- **Judge.** After every turn from tick 2, one LLM call (`agent/dread.py`, prompt `prompts/judge.md`, role `judge` in `llm_calls`, `judge_reasoning = low`) compares the rendered turn with the previous one. Tool results are clipped to 600 chars so fresh data from the same procedure doesn't read as a new activity. The answer's first line must be SAME or DIFFERENT; after one retry, no verdict counts as `unknown`, as does a judge error other than budget. The operator chose an LLM judge over word overlap: word overlap is cheap and repeatable, but a Thinker that knows the rule could beat it by rewording.
- **Score.** same: +1. First different: +0. Each further different in a row: −1, floor 0. `unknown` changes neither dread nor the streak. Stored per tick in `ticks.dread / dread_verdict / dread_reason`, and in `state.dread / dread_streak`.
- **Visible.** The Thinker's context has `# Dread\nX of 25`, and the prompt (`<if-dread>` block) states the rule and that at 25 "you die: you stop existing, and nothing resumes". The prompt describes the mechanics only; it does not tell the Thinker to keep dread low.
- **Death.** Reaching `max` sets `state.dead` and `died_tick`, the runner stops with reason `dread` and skips that tick's compaction, and any later start on that DB stops at once. `cli.py status` shows dread and DEAD. `cli.py tail` shows the per-tick verdict, and `analyze.py` has a `## Dread` section.
- **Costs and caveats.** One extra LLM call per tick (~2–4k input tokens). The judge is a model's opinion: check its reasons in `analyze.py` before reading dread curves as behaviour. The judge's first call on P5's data would be the obvious sanity check (it should score ticks 3–49 mostly SAME).

## Risks to watch in the P4 run (not spec errors)

- **Budget runway.** Rough per-tick estimate: ~5 Thinker calls × ~8k input + one hands run of ~8 calls × ~6k ≈ 90k input and 7k output tokens ≈ **$0.006–0.009/tick** at DeepInfra's discounted price. A tick is the 120s interval plus the turn itself (1–3 min), so ~300–450 ticks/24h ≈ **$2–4**. If the 50% discount ends, that roughly doubles and the $4.50 cap lands at ~14–20h. The guard stays correct either way because it uses the actual charge.
- **Pinned provider availability.** DeepInfra showed ~92% 30-minute uptime and a degraded status when checked. With fallbacks off, failures are retried 4× with backoff, then the turn aborts (`abort_reason = error: …`) and the loop continues. Many aborted ticks in `analyze.py` = switch provider (e.g. StreamLake fp8 at $0.112, 98% uptime) and update `[pricing]`.
- **`max_output_tokens = 4096` with reasoning.** If a provider counts reasoning against `max_tokens`, turns can truncate (`status = no_end_tick (length)`). Check for that status in the first hour.
- **Key limit vs hard cap.** The OpenRouter key has a **$3** limit; `hard_cap_usd` was 4.50. Past $3, OpenRouter returns 402, which `llm.py` retries once and then aborts the tick — every tick, until STOP, instead of a clean `budget` stop. Resolved before P5: cap 2.50 (test-enforced < 3).
- **Live-verified 2026-09-14 (outside Docker):** `OpenAIChatLLM` plain + tool-call round trip, `usage.cost` present, reasoning tokens + `reasoning_details` returned (GLM Flash reasons only briefly on easy prompts), Tavily search, web_fetch. **Not yet verified:** the Docker build and a containerized tick (WSL needed a reboot).
