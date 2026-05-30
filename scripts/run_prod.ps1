# Production runner (quick-tunnel mode).
#
# Starts FastAPI serving the React build + opens a Cloudflare quick tunnel.
# The tunnel URL is printed at startup; bookmark it. URL changes only when the
# tunnel restarts.
#
# Prerequisites (one-time):
#   1. cd web; npm install; npm run build
#   2. Create .env.prod (gitignored) with:
#        WEB_STATIC_DIR=d:\Misc\Vigil\web\dist
#        AUTH_USERNAME=<pick one>
#        AUTH_PASSWORD=<long random string>
#   3. cloudflared installed (already at C:\Program Files (x86)\cloudflared\)
#
# Run from project root:  .\scripts\run_prod.ps1
# Stop with Ctrl-C — both jobs are cleaned up.

param(
    [int]$Port = 8800
)

$ErrorActionPreference = "Stop"

# Load .env.prod into the process environment. We use a separate file from
# .env so test/dev runs don't pick up AUTH_* and start 401-ing everything.
if (Test-Path ".\.env.prod") {
    Get-Content ".\.env.prod" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+?)\s*=\s*(.*)$") {
            $key = $Matches[1].Trim()
            $val = $Matches[2].Trim().Trim('"').Trim("'")
            Set-Item -Path "env:$key" -Value $val
        }
    }
} else {
    Write-Host "WARNING: .env.prod not found at project root." -ForegroundColor Yellow
}

if (-not (Test-Path ".\web\dist\index.html")) {
    Write-Host "web\dist not found. Building React bundle..." -ForegroundColor Yellow
    Push-Location ".\web"
    npm run build
    Pop-Location
}

if (-not $env:WEB_STATIC_DIR) {
    $env:WEB_STATIC_DIR = (Resolve-Path ".\web\dist").Path
}

if (-not $env:AUTH_USERNAME -or -not $env:AUTH_PASSWORD) {
    Write-Host "WARNING: AUTH_USERNAME / AUTH_PASSWORD not set - API will be public!" -ForegroundColor Red
    Write-Host "Set them in .env.prod or environment before exposing the tunnel." -ForegroundColor Red
    $resp = Read-Host "Continue anyway? (y/N)"
    if ($resp -ne "y") { exit 1 }
} else {
    Write-Host "Auth enabled. Username: $env:AUTH_USERNAME" -ForegroundColor Green
}

Write-Host "Starting uvicorn on http://127.0.0.1:$Port ..." -ForegroundColor Cyan
$uvi = Start-Process -PassThru -NoNewWindow `
    -FilePath ".venv\Scripts\python.exe" `
    -ArgumentList "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "$Port"

Start-Sleep -Seconds 2

Write-Host "Opening Cloudflare quick tunnel ..." -ForegroundColor Cyan
Write-Host "(the URL you want is the *.trycloudflare.com line below)" -ForegroundColor Cyan

try {
    & "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:$Port
} finally {
    Write-Host "`nShutting down uvicorn..." -ForegroundColor Yellow
    if ($uvi -and -not $uvi.HasExited) {
        Stop-Process -Id $uvi.Id -Force -ErrorAction SilentlyContinue
    }
}
