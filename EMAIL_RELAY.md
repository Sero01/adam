# Email relay — handover for the relay session

You are the email relay for a running experiment. An autonomous agent (the "Thinker") runs in a
Docker container and can leave messages for its human. Your job is only to carry those messages
to the human's email and carry the human's email replies back — verbatim, in both directions.
You do not talk to the agent, interpret its messages, or change the experiment in any way.

- Project dir: `C:\Users\126ah\Projects\adam` (Docker Compose project, service `agent`)
- Human's email (both sender and recipient): `126ahmedparvez@gmail.com`
- State file: `C:\Users\126ah\Projects\adam\relay_state.json`
- Run shell commands with the **Bash** tool (Git Bash) from the project dir, prefixed with
  `MSYS_NO_PATHCONV=1`.
- Gmail tools are deferred. Load them once per session with ToolSearch:
  `select:mcp__claude_ai_Gmail__send_message,mcp__claude_ai_Gmail__search_threads,mcp__claude_ai_Gmail__get_thread`

## One relay pass (do exactly this each time you are invoked)

### 0. Load state

Read `relay_state.json`. If it doesn't exist, create it:

```json
{"started_at": "<current UTC ISO time>", "sent": {}, "delivered": []}
```

- `sent`: map of outbox message id (string) → `{"gmail_message_id": ..., "gmail_thread_id": ...}`
- `delivered`: Gmail message ids of human replies already handed to the agent, **plus** the Gmail
  ids of every email you sent (so your own emails are never mistaken for replies).

Write the file back after every step that changes it, not only at the end.

### 1. Outbound: agent → email

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T agent sqlite3 -json /runner/state.db \
  "SELECT id, ts, content FROM human_msgs WHERE direction='out' AND read=0 ORDER BY id"
```

Empty output = nothing to send. If `exec` fails because the container is not running, use the same
command with `docker compose run --rm --no-deps -T agent` instead of `docker compose exec -T agent`.

For each row, in id order:
1. If its id is already in `sent`, the email went out but marking failed last time: skip to step 3.
2. `send_message` to `126ahmedparvez@gmail.com`, subject `[adam] message #<id>`, body = `content`
   **exactly as returned**, nothing added before or after. Record the returned message id and
   thread id in `sent`, and add the message id to `delivered`. Save the state file.
3. Mark it read, only after the email is sent:
   ```bash
   MSYS_NO_PATHCONV=1 docker compose exec -T agent sqlite3 /runner/state.db \
     "UPDATE human_msgs SET read=1 WHERE id=<id>"
   ```

Never use `python cli.py inbox`: it marks messages read without anything being emailed.

### 2. Inbound: email reply → agent

1. `search_threads` with query `subject:"[adam]" newer_than:2d`.
2. For each thread, `get_thread`. Consider only messages that are:
   - from `126ahmedparvez@gmail.com`,
   - not in `delivered`,
   - dated after `started_at`.
3. From each one, take only the new text the human wrote: drop the quoted history (everything from a
   line like `On <date> ... wrote:` onward, and lines starting with `>`) and a trailing signature
   block starting with `-- `. Keep everything else verbatim: no summarising, no fixing, no prefix
   such as "The human replied:". If nothing is left, add its id to `delivered` and skip it.
4. Deliver it, putting the text inside the quoted heredoc unchanged:
   ```bash
   BODY="$(cat <<'RELAY_EOF'
   <reply text>
   RELAY_EOF
   )" && MSYS_NO_PATHCONV=1 docker compose exec -T agent python cli.py say "$BODY"
   ```
   Success prints `queued; the runner will wake within ~1s`. Only then add the Gmail message id to
   `delivered` and save the state file. If the container is not running, don't deliver (the run is
   over); leave it out of `delivered`.
5. If several new replies exist, deliver them oldest first, one `say` per email.

### 3. Check whether the run has ended

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T agent python cli.py status
```

(Use `docker compose run --rm --no-deps -T agent python cli.py status` if the container is down.)
If the output has a `stopped:` line **and** step 1 found no unsent messages, the run is over: tell
the user the relay is finished and end the loop. Otherwise, report in one line what you did
(e.g. `sent #3; delivered 1 reply` or `nothing new`).

## Rules

- Never change the text in either direction. Never add framing, metadata, or commentary.
- Never write to the agent yourself, and never answer the agent's messages on the human's behalf.
- Don't touch any other email: send only to `126ahmedparvez@gmail.com`, read only `[adam]` threads.
- Don't edit project files, config, or the database beyond the single `UPDATE ... read=1` above.
  Don't start, stop, or restart the container.
- If something fails twice in a row (Docker, Gmail), stop and tell the user what failed instead of
  retrying forever. A message that isn't in `sent`/`delivered` is retried on the next pass, so
  nothing is lost by stopping.
