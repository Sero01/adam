# Host-side insurance for the unattended 24h run: every hour, a consistent snapshot of
# /runner/state.db into .\backups\, plus `cli.py status` (incl. heartbeat) appended to
# .\backups\status.log. Exits after a final raw copy once the container has stopped.
#
#   Start-Process powershell -WindowStyle Minimized -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File backup_loop.ps1'
#
# Uses sqlite's .backup inside the container, not a plain cp: the runner writes in WAL mode,
# so copying state.db alone can miss recent commits (still in state.db-wal) or catch a torn page.
param([int]$IntervalMin = 60, [string]$Dest = "backups")

Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force $Dest | Out-Null
$log = Join-Path $Dest "status.log"

function Stamp { (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmm") }

while ($true) {
    $stamp = Stamp
    $running = docker compose ps --status running -q agent
    if (-not $running) {
        # runner is gone: no writer left, so the raw files are consistent
        foreach ($f in "state.db", "state.db-wal", "state.db-shm") {
            docker compose cp "agent:/runner/$f" (Join-Path $Dest "final-$stamp-$f") 2>$null | Out-Null
        }
        Add-Content -Encoding utf8 $log "=== $stamp UTC  container not running; final copy taken, backup loop exiting"
        break
    }
    docker compose exec -T agent sqlite3 /runner/state.db ".backup /runner/backup.db"
    $ok = $LASTEXITCODE -eq 0
    if ($ok) {
        docker compose cp agent:/runner/backup.db (Join-Path $Dest "state-$stamp.db") 2>$null | Out-Null
        $ok = $LASTEXITCODE -eq 0
        docker compose exec -T agent rm -f /runner/backup.db
    }
    $status = (docker compose exec -T agent python cli.py status | Out-String).TrimEnd()
    Add-Content -Encoding utf8 $log "=== $stamp UTC  backup $(if ($ok) { 'ok' } else { 'FAILED' })`n$status"
    Start-Sleep -Seconds ($IntervalMin * 60)
}
