<#
.SYNOPSIS
  Let devices on your home network (an Android TV) reach the OpenSeek container, without exposing it any wider.

.DESCRIPTION
  Detects your private LAN address, publishes the owner API on THAT address only (never 0.0.0.0), starts the container, and then
  verifies with `docker inspect` that the published binding is exactly that address. If it is not, the container is stopped again.
  Public, CGNAT, link-local and loopback addresses are refused. Nothing else on your computer or router is changed.

  .\tools\docker-lan.ps1                     detect the LAN address, publish on it, start
  .\tools\docker-lan.ps1 -Ip 192.168.1.20    use this address (it must be one of your own private addresses)
  .\tools\docker-lan.ps1 -Port 18787         use a different host port (default 8787)
  .\tools\docker-lan.ps1 -Off                back to this computer only (127.0.0.1)
#>
param([string]$Ip = "", [int]$Port = 8787, [switch]$Off)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Test-PrivateLan([string]$a) {
    $o = $a.Split('.')
    if ($o.Count -ne 4) { return $false }
    foreach ($x in $o) { if ($x -notmatch '^\d{1,3}$' -or [int]$x -gt 255) { return $false } }
    $b0 = [int]$o[0]; $b1 = [int]$o[1]
    return ($b0 -eq 10) -or ($b0 -eq 172 -and $b1 -ge 16 -and $b1 -le 31) -or ($b0 -eq 192 -and $b1 -eq 168)
}

if ($Off) { $Ip = "127.0.0.1" }
elseif (-not $Ip) {
    $cands = @(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } |
        ForEach-Object { $_.IPv4Address.IPAddress } | Where-Object { Test-PrivateLan $_ })
    if ($cands.Count -eq 0) { throw "No private LAN address found. Pass one explicitly with -Ip (for example 192.168.1.20)." }
    if ($cands.Count -gt 1) { throw "Several private addresses found ($($cands -join ', ')); choose one with -Ip." }
    $Ip = $cands[0]
}
if ($Ip -ne "127.0.0.1" -and -not (Test-PrivateLan $Ip)) {
    throw "$Ip is not a private LAN address (10.x, 172.16-31.x, 192.168.x). The owner API must never be published on it."
}
if ($Port -lt 1024 -or $Port -gt 65535) { throw "-Port must be 1024..65535" }

Set-Content -Path (Join-Path $root ".env") -Encoding ascii -Value @("OPENSEEK_LAN_IP=$Ip", "OPENSEEK_HOST_PORT=$Port")
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose failed" }

# verify what Docker really published (fail closed)
$id = (docker compose ps -q openseek).Trim()
$ports = docker inspect --format '{{json .NetworkSettings.Ports}}' $id | ConvertFrom-Json
$bound = @($ports.PSObject.Properties | ForEach-Object { $_.Value } | Where-Object { $_ } | ForEach-Object { $_ } | ForEach-Object { $_.HostIp })
if ($bound.Count -eq 0 -or ($bound | Where-Object { $_ -ne $Ip })) {
    docker compose down
    throw "Docker published the port on [$($bound -join ', ')] instead of only $Ip. The container was stopped."
}

if ($Ip -eq "127.0.0.1") {
    Write-Host "OpenSeek is reachable from this computer only: https://127.0.0.1:$Port"
} else {
    Write-Host "OpenSeek is published on your LAN address only: https://${Ip}:$Port"
    Write-Host "Pair a device:   docker compose exec openseek python -m openseek pair"
    Write-Host "On the TV enter the server address $Ip (port $Port) and the code shown. Do not forward this port from your router."
}
