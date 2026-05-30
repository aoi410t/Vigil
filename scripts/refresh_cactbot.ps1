# Refresh vendored cactbot timeline files from upstream.
#
# Cactbot updates timelines periodically (especially during active prog seasons).
# This script re-fetches every file under vendor/cactbot/ from the OverlayPlugin
# repo and reports per-file size diffs.
#
# Paths are pinned to specific upstream locations — if cactbot reorganizes,
# update the $files hashtable. Confirm against the live tree:
#   https://github.com/OverlayPlugin/cactbot/tree/main/ui/raidboss/data
#
# Usage:  .\scripts\refresh_cactbot.ps1            # update all
#         .\scripts\refresh_cactbot.ps1 -DryRun    # show what would change
#         .\scripts\refresh_cactbot.ps1 -Only futures_rewritten.txt
#
# After a refresh, re-run the annotator for any affected encounter so the new
# labels propagate to fight_model:
#   POST /api/encounters/{id}/fight-model/annotate-cactbot
# or the bootstrap-equivalent in a Python REPL:
#   from ingest.cactbot import annotate_fight_model_for_encounter
#   annotate_fight_model_for_encounter(session, encounter_id=1079)

param(
    [switch]$DryRun,
    [string]$Only = ""
)

$ErrorActionPreference = "Stop"

# local filename ->upstream raw URL.
# Verified against the live repo tree on 2026-05-24.
$files = [ordered]@{
    "r9s.txt"  = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/ui/raidboss/data/07-dt/raid/r9s.txt"
    "r10s.txt" = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/ui/raidboss/data/07-dt/raid/r10s.txt"
    "r11s.txt" = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/ui/raidboss/data/07-dt/raid/r11s.txt"
    "r12s.txt" = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/ui/raidboss/data/07-dt/raid/r12s.txt"
    "futures_rewritten.txt"          = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/ui/raidboss/data/07-dt/ultimate/futures_rewritten.txt"
    "the_omega_protocol.txt"         = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/ui/raidboss/data/06-ew/ultimate/the_omega_protocol.txt"
    "dragonsongs_reprise_ultimate.txt" = "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/ui/raidboss/data/06-ew/ultimate/dragonsongs_reprise_ultimate.txt"
}

$vendorDir = Join-Path (Split-Path -Parent $PSScriptRoot) "vendor\cactbot"
if (-not (Test-Path $vendorDir)) {
    Write-Host "vendor/cactbot directory not found at $vendorDir" -ForegroundColor Red
    exit 1
}

$updated = 0
$unchanged = 0
$failed = 0

foreach ($name in $files.Keys) {
    if ($Only -and $name -ne $Only) { continue }
    $url  = $files[$name]
    $dest = Join-Path $vendorDir $name
    $oldSize = if (Test-Path $dest) { (Get-Item $dest).Length } else { 0 }

    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -ErrorAction Stop
        $newSize = $resp.RawContentLength
        if ($DryRun) {
            $delta = $newSize - $oldSize
            $sign = if ($delta -ge 0) { "+" } else { "" }
            Write-Host ("DRY  {0,-35}  {1,8} ->{2,8} bytes ({3}{4})" -f $name, $oldSize, $newSize, $sign, $delta)
        } else {
            $resp.Content | Set-Content -Path $dest -Encoding UTF8 -NoNewline
            $finalSize = (Get-Item $dest).Length
            $delta = $finalSize - $oldSize
            if ($delta -eq 0) {
                Write-Host ("OK   {0,-35}  unchanged ({1} bytes)" -f $name, $finalSize) -ForegroundColor DarkGray
                $unchanged++
            } else {
                $sign = if ($delta -ge 0) { "+" } else { "" }
                Write-Host ("OK   {0,-35}  {1,8} ->{2,8} bytes ({3}{4})" -f $name, $oldSize, $finalSize, $sign, $delta) -ForegroundColor Green
                $updated++
            }
        }
    } catch {
        Write-Host ("FAIL {0,-35}  {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
        $failed++
    }
}

if (-not $DryRun) {
    Write-Host ""
    Write-Host ("Summary: {0} updated, {1} unchanged, {2} failed" -f $updated, $unchanged, $failed)
    if ($updated -gt 0) {
        Write-Host "Re-annotate affected encounters via POST /api/encounters/{id}/fight-model/annotate-cactbot" -ForegroundColor Cyan
    }
}
