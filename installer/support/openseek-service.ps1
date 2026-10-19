<#
  OpenSeek installer support script. The installer runs it ELEVATED; users never need to run it.

    openseek-service.ps1 -Action Install   -InstallDir <dir> -User <windows user> [-Port N] [-AdminPort N] [-Version 0.2.0]
    openseek-service.ps1 -Action Uninstall -InstallDir <dir> [-DeleteData]

  Install: sets OpenSeek up as an automatic Windows service running under its own restricted virtual account (NT SERVICE\OpenSeek),
  creates a private data folder, a LAN-only firewall rule, lets the installing user start/stop the service and read the data (for the
  desktop app), picks free ports if the defaults are taken, and waits until the server answers.
  It never opens router ports, never enables internet sharing and never exposes the admin port (it binds 127.0.0.1 only).
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet("Install", "Uninstall")][string]$Action,
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [string]$User = "",
    [int]$Port = 0,
    [int]$AdminPort = 0,
    [string]$Version = "0.2.0",
    [switch]$DeleteData
)
$ErrorActionPreference = "Stop"
$Svc = "OpenSeek"
$Root = Join-Path $env:ProgramData "OpenSeek"
$Data = Join-Path $Root "data"
$Cfg = Join-Path $Root "config.json"
$Info = Join-Path $Root "install.json"
$SvcDir = Join-Path $InstallDir "service"
$Wrapper = Join-Path $SvcDir "OpenSeekService.exe"
$ServerExe = Join-Path $InstallDir "server\OpenSeekServer.exe"
$RuleName = "OpenSeek (home network)"
$Log = Join-Path $env:TEMP "openseek-setup.log"
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Add-Content -Path $Log -Value $line
    Write-Host $line
}
function Test-PortFree([int]$p) { return -not (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue) }
function Pick-Port([int[]]$candidates, [int[]]$avoid) {
    foreach ($c in $candidates) { if (($avoid -notcontains $c) -and (Test-PortFree $c)) { return $c } }
    throw "No free network port found for OpenSeek."
}
function Service-Exists { return [bool](Get-Service -Name $Svc -ErrorAction SilentlyContinue) }
function Stop-OpenSeekService {
    if (Service-Exists) {
        & sc.exe stop $Svc | Out-Null
        $end = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $end -and (Get-Service $Svc).Status -ne "Stopped") { Start-Sleep -Milliseconds 500 }
    }
}
function Remove-OpenSeekService {
    Stop-OpenSeekService
    if (Service-Exists) {
        if (Test-Path $Wrapper) { & $Wrapper uninstall | Out-Null } else { & sc.exe delete $Svc | Out-Null }
        $end = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $end -and (Service-Exists)) { Start-Sleep -Milliseconds 500 }
    }
}
function Remove-Rule { Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule }
function Wait-Health([int]$p, [int]$secs) {
    $end = (Get-Date).AddSeconds($secs)
    while ((Get-Date) -lt $end) {
        $r = & curl.exe -sk -m 4 "https://127.0.0.1:$p/v1/health" 2>$null
        if ($r -match '"service"\s*:\s*"openseek"') { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

if ($Action -eq "Uninstall") {
    Say "Uninstall: stopping and removing the service"
    Remove-OpenSeekService
    Remove-Rule
    Remove-Item $Info -Force -ErrorAction SilentlyContinue
    if ($DeleteData) {
        Say "Deleting $Root (cache, paired devices, certificate)"
        try { Remove-Item $Root -Recurse -Force } catch { Say "Could not delete everything: $($_.Exception.Message)"; exit 3 }
    } else {
        Say "Keeping your data in $Root"
    }
    exit 0
}

# ------------------------------------------------------------------------------------------------------------------ Install
Say "Install OpenSeek $Version into $InstallDir for user '$User'"
foreach ($p in @($ServerExe, $Wrapper)) { if (-not (Test-Path $p)) { throw "Missing file: $p" } }

# who may control the service and read the data: the installing user
$userSid = $null
foreach ($name in @($User, "$env:USERDOMAIN\$User")) {
    if (-not $name) { continue }
    try { $userSid = (New-Object System.Security.Principal.NTAccount($name)).Translate([System.Security.Principal.SecurityIdentifier]).Value; break } catch { }
}
if (-not $userSid) { throw "Could not determine the Windows account '$User'." }
Say "User SID resolved"

Remove-OpenSeekService
Remove-Rule

New-Item -ItemType Directory -Force $Root | Out-Null
New-Item -ItemType Directory -Force $Data | Out-Null
New-Item -ItemType Directory -Force (Join-Path $Data "logs") | Out-Null

# ports: keep the previous choice on a reinstall, else the defaults, else the first free alternative
$old = @{}
if (Test-Path $Cfg) { try { $old = ConvertFrom-Json ([IO.File]::ReadAllText($Cfg, $Utf8)) | ForEach-Object { $_ } } catch { $old = @{} } }
$wantApi = if ($Port -gt 0) { $Port } elseif ($old.port) { [int]$old.port } else { 8787 }
$wantAdm = if ($AdminPort -gt 0) { $AdminPort } elseif ($old.admin_port) { [int]$old.admin_port } else { 8788 }
$apiPort = Pick-Port (@($wantApi) + 18787, 28787, 38787, 48787) @()
$admPort = Pick-Port (@($wantAdm) + 18788, 28788, 38788, 48788) @($apiPort)
Say "Ports: owner API $apiPort, admin $admPort (127.0.0.1 only)"

# server settings file (the desktop app edits it; the server only honours an allowlist of keys from it)
$conf = [ordered]@{}
if ($old -is [System.Management.Automation.PSCustomObject]) { foreach ($pr in $old.PSObject.Properties) { $conf[$pr.Name] = $pr.Value } }
$conf["port"] = $apiPort
$conf["admin_port"] = $admPort
if (-not $conf.Contains("host")) { $conf["host"] = "0.0.0.0" }
if (-not $conf.Contains("playback_guard")) { $conf["playback_guard"] = $true }
[IO.File]::WriteAllText($Cfg, ((ConvertTo-Json $conf) + "`n"), $Utf8)

# service definition (WinSW wrapper next to its XML)
$xml = @"
<service>
  <id>$Svc</id>
  <name>OpenSeek</name>
  <description>OpenSeek seek-preview thumbnail server (https://github.com/obadevyh/openseek)</description>
  <executable>$ServerExe</executable>
  <arguments>serve</arguments>
  <workingdirectory>$InstallDir\server</workingdirectory>
  <env name="OPENSEEK_DATA_DIR" value="$Data"/>
  <env name="OPENSEEK_CONFIG_FILE" value="$Cfg"/>
  <logpath>$Data\logs</logpath>
  <log mode="roll-by-size"><sizeThreshold>5120</sizeThreshold><keepFiles>6</keepFiles></log>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>15 sec</stoptimeout>
</service>
"@
[IO.File]::WriteAllText((Join-Path $SvcDir "OpenSeekService.xml"), $xml, $Utf8)
& $Wrapper install | Out-Null
if (-not (Service-Exists)) { throw "The Windows service could not be created." }
& sc.exe config $Svc obj= "NT SERVICE\$Svc" | Out-Null          # restricted virtual account, no password
& sc.exe sidtype $Svc unrestricted | Out-Null
& sc.exe privs $Svc "SeChangeNotifyPrivilege/SeCreateGlobalPrivilege" | Out-Null
& sc.exe description $Svc "OpenSeek seek-preview thumbnail server" | Out-Null
Say "Service installed (automatic, delayed start, restarts itself after a crash)"

# data folder: private. SYSTEM + Administrators full, the service modifies, the installing user reads. Nobody else.
$system, $admins = "*S-1-5-18", "*S-1-5-32-544"
& icacls.exe $Root /inheritance:r /grant:r "${system}:(OI)(CI)F" "${admins}:(OI)(CI)F" "*${userSid}:(OI)(CI)RX" "NT SERVICE\${Svc}:(RX)" | Out-Null
& icacls.exe $Data /grant:r "NT SERVICE\${Svc}:(OI)(CI)M" | Out-Null
& icacls.exe $Cfg /grant:r "*${userSid}:M" "NT SERVICE\${Svc}:R" | Out-Null      # the desktop app may edit the settings file
Say "Data folder permissions set (private to SYSTEM, Administrators, the service and $User)"

# the installing user may start/stop/query the service (not reconfigure or delete it)
$sd = ((& sc.exe sdshow $Svc) -join "").Trim()
if ($sd -notmatch [regex]::Escape($userSid)) {
    $ace = "(A;;CCLCSWRPWPDTLOCRRC;;;$userSid)"
    $sd = if ($sd.Contains("S:")) { $sd.Replace("S:", "$ace" + "S:") } else { $sd + $ace }
    & sc.exe sdset $Svc $sd | Out-Null
}
Say "Service control granted to $User"

# firewall: this server program only, TCP on its port, from the local subnet only, on any network profile (home networks are often 'Public')
New-NetFirewallRule -DisplayName $RuleName -Description "Lets devices on your home network reach OpenSeek. Local subnet only; the admin port is never opened." `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $apiPort -Program $ServerExe -RemoteAddress LocalSubnet -Profile Any | Out-Null
Say "Firewall rule created: TCP $apiPort, local subnet only"

$json = [ordered]@{ service = $Svc; data_dir = $Data; config_file = $Cfg; version = $Version; install_dir = $InstallDir }
[IO.File]::WriteAllText($Info, ((ConvertTo-Json $json) + "`n"), $Utf8)

& sc.exe start $Svc | Out-Null
if (Wait-Health $apiPort 45) { Say "OpenSeek is running"; exit 0 }
Say "The service did not answer within 45 seconds. See $Data\logs"
exit 2
