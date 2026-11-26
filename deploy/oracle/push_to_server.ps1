# Copies the trader from this PC to the Oracle Cloud server.
#
# First time (code + database + settings):
#   powershell -ExecutionPolicy Bypass -File deploy\oracle\push_to_server.ps1 -Server ubuntu@<PUBLIC_IP>
#
# Research results only (Backtests tab data; no restart):
#   powershell -ExecutionPolicy Bypass -File deploy\oracle\push_to_server.ps1 -Server ubuntu@<PUBLIC_IP> -Research
#
# Later, to send code changes only (the server keeps its own database, .env and Webull login):
#   powershell -ExecutionPolicy Bypass -File deploy\oracle\push_to_server.ps1 -Server ubuntu@<PUBLIC_IP> -CodeOnly
#
# The full copy refuses to run while the bot is running on this PC (the database must be
# closed) or when the server already has a database (so it never overwrites the server's live data).

param(
    [Parameter(Mandatory = $true)][string]$Server,
    [switch]$CodeOnly,
    # Research results only (reports, trials registry, leaderboards, summaries under data/experiments
    # and data/backtests). Code-only pushes skip data/, so without this the Backtests tab on the server
    # goes stale. No restart: the API reads these files on each request.
    [switch]$Research,
    [string]$Key = "$env:USERPROFILE\.ssh\oracle_trader.key"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$SshOpts = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new")

function Invoke-Remote([string]$Cmd) {
    & ssh @SshOpts $Server $Cmd
    if ($LASTEXITCODE -ne 0) { throw "Command on the server failed: $Cmd" }
}

if (-not (Test-Path $Key)) { throw "SSH key not found: $Key (see deploy\oracle\README.md)" }
if (-not (Test-Path (Join-Path $Root "web\out\index.html"))) {
    throw "web\out is missing. Build the dashboard first: cd web; npm run build"
}

if (-not $CodeOnly -and -not $Research) {
    # The database must not be copied while the bot has it open.
    $lockFile = Join-Path $Root "data\trader.lock"
    if (Test-Path $lockFile) {
        $botPid = ((Get-Content $lockFile -Raw).Trim() -split ":")[0]
        $proc = if ($botPid -match '^\d+$') { Get-Process -Id ([int]$botPid) -ErrorAction SilentlyContinue }
        if ($proc -and $proc.ProcessName -like "python*") {
            throw "The bot is still running on this PC (PID $botPid). Run Stop_Trader.bat first."
        }
    }
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
        throw "Something is still listening on port 8000. Run Stop_Trader.bat first."
    }
    # Fail closed: if ssh itself fails we cannot know the server is empty.
    $probe = & ssh @SshOpts $Server "for p in data/trader.db .env conf; do if [ -e ~/trader/`$p ]; then echo EXISTS:`$p; fi; done; echo PROBE_DONE"
    if ($LASTEXITCODE -ne 0) { throw "Could not check the server (ssh exit $LASTEXITCODE); refusing a full push." }
    if (-not ($probe -contains "PROBE_DONE")) { throw "Server check gave no result; refusing a full push." }
    if ($probe -match "^EXISTS:") {
        throw "The server already has data ($(($probe | Where-Object { $_ -like 'EXISTS:*' }) -join ', ')). Use -CodeOnly so its live data is not overwritten."
    }
}

$excludes = @(".venv", "node_modules", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
              "logs", "web/.next", "web/out_prev*", "web/tsconfig.tsbuildinfo",
              "data/trader.lock", "data/api_token", "PC_BOT_DISABLED")
if ($Research) {
    $files = Get-ChildItem -Path (Join-Path $Root "data/experiments"), (Join-Path $Root "data/backtests") -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { ($_.Name -like "*.md" -or $_.Name -eq "trials_registry.csv" -or $_.Name -like "*leaderboard*.csv" -or
                        $_.Name -like "summary*.json" -or $_.Name -eq "results.json") -and
                       $_.FullName -notmatch '[\\/]_cache[\\/]' -and $_.Name -notmatch "backup" } |
        ForEach-Object { "./" + ($_.FullName.Substring($Root.Length + 1) -replace "\\", "/") }
    if (-not $files) { throw "No research files found under data/experiments or data/backtests" }
    $list = Join-Path $env:TEMP "trader_research_files.txt"
    [IO.File]::WriteAllLines($list, [string[]]$files)
    $archive = Join-Path $env:TEMP "trader_research.tgz"
    if (Test-Path $archive) { Remove-Item $archive -Force }
    & tar.exe -czf $archive -C $Root -T $list
    if ($LASTEXITCODE -ne 0) { throw "tar failed" }
    Write-Host ("Research files: {0}, archive {1:N0} KB" -f $files.Count, ((Get-Item $archive).Length / 1KB))
    try {
        & scp @SshOpts $archive "${Server}:trader_research.tgz"
        if ($LASTEXITCODE -ne 0) { throw "Upload failed" }
        Invoke-Remote "mkdir -p ~/trader && tar -xzf ~/trader_research.tgz -C ~/trader && rm ~/trader_research.tgz && echo 'Research results synced (no restart needed).'"
    } finally {
        Remove-Item $archive, $list -Force -ErrorAction SilentlyContinue
    }
    return
}

if ($CodeOnly) { $excludes += @("data", "conf", ".env") }

$archive = Join-Path $env:TEMP "trader_push.tgz"
if (Test-Path $archive) { Remove-Item $archive -Force }
$tarArgs = @("-czf", $archive)
foreach ($e in $excludes) { $tarArgs += "--exclude=./$e" }
$tarArgs += @("--exclude=__pycache__", "-C", $Root, ".")

Write-Host "Packing $(if ($CodeOnly) { 'code' } else { 'code, database and settings' })..."
& tar.exe @tarArgs
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
Write-Host ("Archive: {0:N0} MB" -f ((Get-Item $archive).Length / 1MB))

try {
    Write-Host "Uploading to $Server..."
    & scp @SshOpts $archive "${Server}:trader_push.tgz"
    if ($LASTEXITCODE -ne 0) { throw "Upload failed" }

    Write-Host "Unpacking on the server..."
    Invoke-Remote ("mkdir -p ~/trader && tar -xzf ~/trader_push.tgz -C ~/trader && rm ~/trader_push.tgz" +
        " && find ~/trader -name '*.sh' -not -path '*/.venv/*' -exec sed -i 's/\r$//' {} +" +
        " && if [ -f ~/trader/.env ]; then sed -i 's/\r$//' ~/trader/.env && chmod 600 ~/trader/.env; fi")

    if ($CodeOnly) {
        # Back up the live DB first; abort (no restart) if the backup fails.
        Invoke-Remote ("cd ~/trader && if [ -f data/trader.db ] && [ -x .venv/bin/python ]; then .venv/bin/python scripts/backup_db.py data/trader.db --keep 10 || exit 1; fi")
        Invoke-Remote ("cd ~/trader && if [ ! -x .venv/bin/python ]; then echo 'Not set up yet: run  bash deploy/oracle/server_setup.sh  on the server.'; exit 0; fi" +
            " && .venv/bin/python -m pip install -q -r requirements.txt" +
            " && if systemctl --user is-active --quiet ai-trader; then systemctl --user restart ai-trader && echo 'Bot restarted with the new code.';" +
            " else echo 'Bot is not running; start it with: systemctl --user start ai-trader'; fi")
    } else {
        Write-Host ""
        Write-Host "Done. Next, on the server (ssh -i $Key $Server):"
        Write-Host "  cd ~/trader && bash deploy/oracle/server_setup.sh   (only if not done before)"
        Write-Host "  ./auth.sh && systemctl --user start ai-trader"
    }
} finally {
    Remove-Item $archive -Force -ErrorAction SilentlyContinue
}
