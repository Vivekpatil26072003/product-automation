# Starts the whole app on this PC for phones and computers on the same Wi-Fi / office network.
#   powershell -ExecutionPolicy Bypass -File infra\local\start-office.ps1
# Steps: Docker services -> database migration -> storage bucket -> API, worker, website (production build).
# The PC's network address is found on every start, so the link still works when the router gives a new address.
# Phones upload photos straight to the file storage (port 9000), so storage is addressed by that network address.
# Secrets stay in .env (never in this script). Logs: infra\local\logs\*.log

# Native tools (docker, npm) write progress to stderr: failures are checked through their exit codes instead.
$ErrorActionPreference = "Continue"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $root ".venv\Scripts\python.exe"
$web = Join-Path $root "apps\web"
$logs = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force $logs | Out-Null

function Say($text) { Write-Host "[app] $text" }

# 1. This PC's address on the network (the adapter with a default gateway: Wi-Fi or office cable).
$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
       Select-Object -First 1).IPv4Address.IPAddress
if (-not $ip) { $ip = "localhost"; Say "No network found: the app will open on this PC only." }
Say "Network address: $ip"

# 2. Docker Desktop and the services (database, Redis, file storage, virus scanner).
cmd /c "docker info >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Say "Starting Docker Desktop..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    for ($i = 0; $i -lt 60; $i++) { Start-Sleep 5; cmd /c "docker info >nul 2>&1"; if ($LASTEXITCODE -eq 0) { break } }
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop did not start. Open it by hand and run this script again." }
}
Set-Location $root
docker compose -f infra/local/docker-compose.yml up -d --wait
if ($LASTEXITCODE -ne 0) { throw "The Docker services did not start (see: docker compose -f infra/local/docker-compose.yml ps)." }

# 3. Settings for this start (they override .env for these processes only; .env is not changed).
$env:PYTHONPATH = "services/api;services"
$env:STORAGE_ENDPOINT_URL = "http://${ip}:9000"
$env:STORAGE_CORS_ORIGINS = "[`"http://${ip}:3000`",`"http://localhost:3000`"]"
$env:STORAGE_PUBLIC_ORIGIN = "http://${ip}:9000"
$env:BACKEND_URL = "http://127.0.0.1:8000"
$env:NEXT_PUBLIC_DEV_AUTH = "true"

& $python -m app.cli migrate
& $python -m app.cli ensure-bucket   # allows uploads from http://<this PC>:3000

# 4. Stop an earlier start of the app (API, worker, website).
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match "uvicorn app.main:app|-m workers" -or
    ($_.CommandLine -match "next" -and $_.CommandLine -match [regex]::Escape($web))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

# 5. Website: a production build, rebuilt only when the code or the network address changed.
$stamp = Join-Path $web ".next\office-build.txt"
$head = (git -C $root rev-parse HEAD 2>$null) + (git -C $root status --porcelain 2>$null | Out-String).GetHashCode()
$want = "$ip|$head"
if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $want) {
    Say "Building the website (about a minute)..."
    Push-Location $web
    $env:NODE_ENV = "production"
    npm run build *> (Join-Path $logs "web-build.log")
    $built = $LASTEXITCODE
    Remove-Item Env:NODE_ENV
    Pop-Location
    if ($built -ne 0) { throw "Website build failed: see infra\local\logs\web-build.log" }
    Set-Content $stamp $want
}

# 6. Start API (this PC only; the website forwards /api), workers, website (whole network).
Start-Process $python -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs "api.log") -RedirectStandardError (Join-Path $logs "api-errors.log")
# Several workers: photos sent together are read at the same time (each job is taken by one worker only).
foreach ($n in 1..3) {
    Start-Process $python -ArgumentList "-m", "workers" -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "worker-$n.log") -RedirectStandardError (Join-Path $logs "worker-$n-errors.log")
}
Start-Process "cmd.exe" -ArgumentList "/c", "npx next start -H 0.0.0.0 -p 3000" -WorkingDirectory $web -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs "web.log") -RedirectStandardError (Join-Path $logs "web-errors.log")

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep 2
    try { if ((Invoke-WebRequest -UseBasicParsing "http://${ip}:3000/login" -TimeoutSec 5).StatusCode -eq 200) { break } } catch {}
}
Say "Ready. Open on phones and computers on the same Wi-Fi:  http://${ip}:3000"
Say "Keep this PC on (no sleep). To stop: infra\local\stop-office.ps1"
