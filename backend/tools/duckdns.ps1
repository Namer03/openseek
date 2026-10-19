<#
  DuckDNS dynamic-IP updater for the OpenSeek public endpoint. Nothing here opens a port or touches the firewall/router.

    tools\duckdns.ps1 -Setup  -Domain myname        # asks for the DuckDNS token (hidden), stores it, installs a 5-minute task
    tools\duckdns.ps1 -Update                       # what the scheduled task runs (also fine to run by hand)
    tools\duckdns.ps1 -Status                       # shows task state, stored name, last result (never the token)
    tools\duckdns.ps1 -Remove                       # removes the task and the stored token

  TOKEN HANDLING. The token is stored with Windows DPAPI (ConvertFrom-SecureString without a key): only THIS Windows user on
  THIS PC can decrypt it, the file is ACL-restricted to that user, and it is never written to logs, the task definition or
  the console. DuckDNS only offers the token in the update URL, so the request goes over HTTPS and the URL is never logged.
  -Domain is the sub-domain WITHOUT ".duckdns.org". IPv4 only (an empty ip= lets DuckDNS use the address it sees).
#>
param(
  [switch]$Setup, [switch]$Update, [switch]$Status, [switch]$Remove,
  [string]$Domain = "",
  [int]$EveryMinutes = 5,
  [string]$FromFile = "",                    # -Setup: read the token from this file instead of asking (delete the file afterwards)
  [string]$StateDir = "",                     # tests only
  [string]$TaskName = "OpenSeek DuckDNS update"
)
$ErrorActionPreference = "Stop"
$dir = if ($StateDir) { $StateDir } else { Join-Path $env:LOCALAPPDATA "OpenSeek" }
$tokenFile = Join-Path $dir "duckdns.token"
$confFile = Join-Path $dir "duckdns.conf"
$logFile = Join-Path $dir "duckdns.log"
$taskName = $TaskName

function Log($msg) {
  New-Item -ItemType Directory -Force $dir | Out-Null
  if ((Test-Path $logFile) -and (Get-Item $logFile).Length -gt 200KB) { Move-Item -Force $logFile "$logFile.old" }
  Add-Content -Path $logFile -Value ("{0:u} {1}" -f (Get-Date), $msg)
}
function Restrict($path) {                       # this user only
  $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  icacls $path /inheritance:r /grant:r "${me}:(F)" | Out-Null
}
function Read-Token {
  $sec = ConvertTo-SecureString (Get-Content -Raw $tokenFile).Trim()
  $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

if ($Setup) {
  if ($Domain -notmatch '^[a-z0-9][a-z0-9-]{0,62}$') { throw "-Domain must be the sub-domain only, e.g. myname (for myname.duckdns.org)" }
  New-Item -ItemType Directory -Force $dir | Out-Null
  if ($FromFile) { $sec = ConvertTo-SecureString ((Get-Content -Raw $FromFile).Trim()) -AsPlainText -Force }
  else { $sec = Read-Host "DuckDNS token (input hidden)" -AsSecureString }
  if ($sec.Length -lt 20) { throw "that does not look like a DuckDNS token" }
  $sec | ConvertFrom-SecureString | Set-Content -Path $tokenFile -Encoding ascii
  Set-Content -Path $confFile -Value $Domain -Encoding ascii
  Restrict $tokenFile; Restrict $confFile
  $ps = (Get-Command powershell.exe).Source
  $extra = if ($StateDir) { " -StateDir `"$StateDir`"" } else { "" }
  $act = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Update$extra"
  # two triggers: at every log-on (so it also runs right after a restart) and every N minutes from then on
  $trg = @((New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)),
           (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)))
  $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
  Register-ScheduledTask -TaskName $taskName -Action $act -Trigger $trg -Settings $set -Description "Keeps $Domain.duckdns.org pointing at this PC's public IP" -Force | Out-Null
  Write-Host "stored the token (DPAPI, this user only) and installed '$taskName' every $EveryMinutes min. Running one update now..."
  & $PSCommandPath -Update -StateDir $StateDir
  return
}

if ($Update) {
  try {
    if (-not (Test-Path $tokenFile) -or -not (Test-Path $confFile)) { Log "not set up (run -Setup)"; exit 2 }
    $name = (Get-Content -Raw $confFile).Trim()
    $tok = Read-Token
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $r = Invoke-RestMethod -Uri ("https://www.duckdns.org/update?domains={0}&token={1}&ip=" -f $name, $tok) -TimeoutSec 20 -UseBasicParsing
    $tok = $null
    $ok = ("$r").Trim() -eq "OK"
    Log ("update {0}.duckdns.org -> {1}" -f $name, $(if ($ok) { "OK" } else { "KO (check the name and token)" }))
    if (-not $ok) { exit 1 }
  } catch {
    Log ("update failed: " + $_.Exception.GetType().Name)      # the message can contain the request URL: not logged
    exit 1
  }
  return
}

if ($Status) {
  $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  Write-Host ("task: {0}" -f $(if ($t) { $t.State } else { "not installed" }))
  if (Test-Path $confFile) { Write-Host ("name: {0}.duckdns.org" -f (Get-Content -Raw $confFile).Trim()) }
  Write-Host ("token stored: {0}" -f (Test-Path $tokenFile))
  if (Test-Path $logFile) { Write-Host "last log lines:"; Get-Content $logFile -Tail 5 }
  return
}

if ($Remove) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  Remove-Item $tokenFile, $confFile -ErrorAction SilentlyContinue
  Write-Host "removed the task and the stored token (the DuckDNS name itself stays; delete it on duckdns.org if you want)"
  return
}
Get-Help $PSCommandPath | Out-Null
Write-Host "use one of: -Setup -Domain <name> | -Update | -Status | -Remove"
