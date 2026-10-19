<#
  Start (or restart) the normal OpenSeek backend on 8787 with the validated everyday settings.

    tools\run_production.ps1                       # multi-range ON (the default of this script)
    tools\run_production.ps1 -MultiRange 0         # same code, multi-range OFF  (the quick switch back)
    tools\run_production.ps1 -Guard 0              # playback guard OFF (it pauses background extraction on rebuffer / CDN stall)
    tools\run_production.ps1 -MultiRange 0 -CodeTag production-pre-multirange   # full code rollback
    tools\run_production.ps1 -PublicHost name.example.org:443                   # ALSO start the guest-only public listener (8789)
    tools\run_production.ps1                                                    # without -PublicHost the public listener is OFF

  It only ever stops the process that LISTENS on 8787 after checking that its command line is "openseek serve" (and its
  launcher parent, if that is one too). The data directory (thumbnail cache, pairing, TLS identity) is never touched.
  Settings: 3 RPS, 64 Mbit/s background cap, full pre-generation, 5 s
  tolerance, 2 workers per source (override with -Workers). Logs: <data dir>\logs\serve-<time>.log (no URLs or tokens).
#>
param(
  [int]$MultiRange = 1,
  [int]$Guard = 1,
  [int]$Workers = 2,
  [string]$CodeTag = "",
  [string]$Data = (Join-Path $env:LOCALAPPDATA "OpenSeek\data"),
  [int]$Port = 8787,
  [string]$PublicHost = "",     # "name.example.org:443": starts the internet-facing GUEST listener (empty = not started, the default)
  [int]$PublicPort = 8789
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$backend = Join-Path $repo "backend"
$py = Join-Path $backend ".venv\Scripts\python.exe"

function Is-OpenSeekServe($procId) {
  $c = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
  return ($null -ne $c -and $c.CommandLine -match "openseek" -and $c.CommandLine -match "\bserve\b" -and $c.CommandLine -match "python")
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
  $pid1 = $listener.OwningProcess
  if (-not (Is-OpenSeekServe $pid1)) { throw "Port $Port is held by process $pid1, which is not 'openseek serve'. Not touching it." }
  $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$pid1").ParentProcessId
  Write-Host "stopping openseek serve pid $pid1 (launcher parent $parent)"
  Stop-Process -Id $pid1 -Force
  if ($parent -and (Is-OpenSeekServe $parent)) { Stop-Process -Id $parent -Force }
  Start-Sleep -Seconds 2
}

if ($CodeTag) {
  git -C $repo checkout --quiet $CodeTag
  Write-Host "code at tag $CodeTag (detached). Back to the current code: git -C $repo checkout <your branch>"
}

$env:OPENSEEK_DATA_DIR = $Data
$env:OPENSEEK_BG_MBPS = "64"
$env:OPENSEEK_PREGEN = "full"
$env:OPENSEEK_TOLERANCE_S = "5"
$env:OPENSEEK_UPSTREAM_RPS = "3"
$env:OPENSEEK_WORKERS_PER_SOURCE = "$Workers"
$env:OPENSEEK_MULTIRANGE_PREFETCH = "$MultiRange"
$env:OPENSEEK_MULTIRANGE_BATCH = "8"
$env:OPENSEEK_PLAYBACK_GUARD = "$Guard"
if ($PublicHost) {
  $env:OPENSEEK_PUBLIC_PORT = "$PublicPort"
  $env:OPENSEEK_PUBLIC_HOST = $PublicHost
} else {
  Remove-Item Env:OPENSEEK_PUBLIC_PORT, Env:OPENSEEK_PUBLIC_HOST -ErrorAction SilentlyContinue
}

$logs = Join-Path $Data "logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$proc = Start-Process -FilePath $py -ArgumentList "-m", "openseek", "serve" -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $logs "serve-$stamp.log") -RedirectStandardError (Join-Path $logs "serve-$stamp.err.log")
Write-Host "started openseek serve pid $($proc.Id): multi-range=$MultiRange guard=$Guard workers=$Workers rps=3 bg=64Mbit/s data=$Data"
Start-Sleep -Seconds 6
$up = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($up) { Write-Host "listening on $Port" } else { Write-Host "NOT listening yet - check $logs\serve-$stamp.err.log" }
