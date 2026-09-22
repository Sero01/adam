# Runbook — P4: sandbox + 1-hour live run

Commands are PowerShell from the project directory. See `SPEC_NOTES.md` for why things differ from the spec.

## 0. Prerequisites (this machine has none of these yet)

1. Docker Desktop for Windows (WSL 2 backend): `winget install Docker.DockerDesktop`, reboot, start Docker Desktop.
2. OpenRouter key with a **$5 credit limit**, created for this run only; rotate afterwards.
3. Search key: Tavily (free tier) → `provider = "tavily"`; or Brave → `"brave"`; or no key → `"duckduckgo"`.

## 1. Configure

```powershell
Copy-Item .env.example .env      # fill in LLM_API_KEY, SEARCH_API_KEY
New-Item -ItemType Directory -Force workspace
```

In `config.toml`: `max_runtime_s = 3600` stops the runner after 1 hour. Before starting, check that the pinned provider is still the one you want:

```powershell
curl.exe -s https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints
```

## 2. Tests, build, start

```powershell
.venv\Scripts\python.exe -m pytest -q
docker compose build
docker compose up -d
docker compose logs -f           # Ctrl+C detaches; the agent keeps running
```

## 3. During the run

```powershell
docker compose exec agent python cli.py status
docker compose exec agent python cli.py tail 10
docker compose exec agent python cli.py inbox
docker compose exec agent python cli.py say "hello"
docker compose exec agent python cli.py stop          # kill switch (or: docker compose stop)
```

The agent's files are visible on the host in `.\workspace\`.

## 4. After the run

```powershell
docker compose exec agent python analyze.py        # if still running; else: docker compose run --rm agent python analyze.py
docker compose cp agent:/runner/analysis ./analysis
docker compose cp agent:/runner/logs ./logs
docker compose cp agent:/runner/state.db ./state.db
```

Read `analysis/report.md`, `logs/thinker.log`, `logs/hands.log`. For the first hour, check specifically:

- `aborted` ticks and their `abort_reason` (provider errors? timeouts?)
- `no_end_tick (length)` statuses → output token limit truncating reasoning
- compaction rows, and that *Current focus / Open threads* survive (`restored_sections`)
- cost per tick × expected ticks/24h vs the $4.50 cap

Per spec §10, adjust only tick interval / summary format, and only if broken.

## 5. P5 — 24-hour run

Save the previous run first (`docker compose cp agent:/runner runs\<name>`, plus a copy of `workspace\`), then:

```powershell
.venv\Scripts\python.exe -m pytest -q
docker compose down -v            # -v deletes the runner-data volume (state.db, logs)
Remove-Item -Recurse -Force workspace; New-Item -ItemType Directory workspace
docker compose build
docker compose up -d
Start-Process powershell -WindowStyle Minimized -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File backup_loop.ps1'
```

`config.toml` for P5: `max_runtime_s = 0`, `hard_cap_usd = 2.50` (key limit $3), `[human] enabled = false`. Keep the PC awake (no sleep on AC) for 24h — a sleeping host pauses Docker Desktop.

During the run:

```powershell
docker compose exec agent python cli.py status     # heartbeat line: STALE = runner hung or dead
Get-Content backups\status.log -Tail 20            # hourly status + backup result
```

After: `docker compose cp agent:/runner runs\p5-24h`, copy `workspace\`, run `analyze.py` on the copy. The last snapshot is also in `backups\`.
