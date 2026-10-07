# Stops the app started by start-office.ps1 (API, worker, website). The Docker services and their data stay.
$web = Resolve-Path (Join-Path $PSScriptRoot "..\..\apps\web")
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match "uvicorn app.main:app|-m workers" -or
    ($_.CommandLine -match "next" -and $_.CommandLine -match [regex]::Escape($web))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "[app] Stopped. Data is kept; start again with infra\local\start-office.ps1"
