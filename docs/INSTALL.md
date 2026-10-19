# Installing OpenSeek

OpenSeek needs Python 3.12 or newer (pins in `backend/requirements.txt`). The ffmpeg libraries are bundled in the `av` wheel.
Pick a data directory outside the source tree and back it up if you want to keep your pairings and cache: it holds the
database, the TLS identity (`tls/`), the capability key (`secret.key`) and the thumbnail cache.

## Windows

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -c requirements-lock.txt
$env:OPENSEEK_DATA_DIR = "$env:LOCALAPPDATA\OpenSeek\data"
.venv\Scripts\python -m openseek serve
```

The server prints its LAN address(es), the certificate pin and the admin URL. Pair a device from a second terminal
(same environment variable): `.venv\Scripts\python -m openseek pair`. Open the local dashboard with `.venv\Scripts\python -m openseek dashboard`.

**Keep the source checkout and the data directory inside your own user profile** (for example `%LOCALAPPDATA%\OpenSeek\data`, the default
used above). A folder at the root of another drive such as `D:\` usually inherits permissions that let every local user read it, and
the data directory holds the TLS private key, the capability key (`secret.key`), the admin secret (`admin.token`) and the invitation database.
If the server finds such a data directory it logs a warning at start-up.

Windows Firewall: when Windows asks, untick **Public networks**. Better, create the rule yourself so that it covers only this Python,
only the Private profile and only your own subnet (run PowerShell as administrator; adjust the path):

```powershell
New-NetFirewallRule -DisplayName "OpenSeek API (LAN only)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8787 `
  -Program "C:\path\to\openseek\backend\.venv\Scripts\python.exe" -Profile Private -RemoteAddress LocalSubnet
```

The router must not forward 8787 or 8788.

### Admin secret

The loopback admin API (port 8788) requires a per-install secret in the `X-OpenSeek-Admin` header. By default the server creates a random one in
`<data>\admin.token` (readable only by you where the OS supports it); `python -m openseek pair|devices|revoke|dashboard` and the desktop app read it from
there. To choose your own, set `OPENSEEK_ADMIN_TOKEN` (at least 16 characters; prefer the file for anything other than automation). The secret protects
against other local users, other local programs and containers that can reach the host's loopback; it is not a substitute for keeping the data directory private.

### Optional: `backend/tools/run_production.ps1`

A convenience script that (re)starts the server in the background with a fixed set of everyday settings (background cap 64 Mbit/s,
full pre-generation, 2 workers per source, request pacing 3/s, multi-range prefetch and playback guard on). Read it before use and adapt
the values to your link.

```powershell
backend\tools\run_production.ps1                                  # start or restart on 8787 (data in %LOCALAPPDATA%\OpenSeek\data)
backend\tools\run_production.ps1 -MultiRange 0 -Guard 0
backend\tools\run_production.ps1 -PublicHost name.example.org:443   # also start the guest listener
```

The desktop app and `duckdns.ps1` run these scripts from the checkout, so keep the checkout somewhere only you can write to.
It only stops a process that listens on the API port and whose command line is `openseek serve`. Logs go to `<data>\logs\`.
It expects the virtual environment at `backend\.venv`. `-CodeTag` checks out a git tag in the repository; do not use it unless you
know you want that.

## Linux

```sh
sudo useradd --system --home-dir /var/lib/openseek --create-home --shell /usr/sbin/nologin openseek
sudo git clone https://github.com/obadevyh/openseek.git /opt/openseek
cd /opt/openseek/backend
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt -c requirements-lock.txt
```

`/etc/systemd/system/openseek.service`:

```ini
[Unit]
Description=OpenSeek seek-preview server
After=network-online.target
Wants=network-online.target

[Service]
User=openseek
WorkingDirectory=/opt/openseek/backend
Environment=OPENSEEK_DATA_DIR=/var/lib/openseek
Environment=OPENSEEK_PLAYBACK_GUARD=1
ExecStart=/opt/openseek/backend/.venv/bin/python -m openseek serve
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/openseek
ProtectHome=true
UMask=0077
CapabilityBoundingSet=
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now openseek
sudo -u openseek env OPENSEEK_DATA_DIR=/var/lib/openseek /opt/openseek/backend/.venv/bin/python -m openseek pair
```

The hardening options in the unit are a sensible starting point; they have not been run on every distribution, so check `systemctl status` and the journal after enabling them.
Firewall: allow TCP 8787 from your LAN only.

## Docker

```sh
docker compose up -d --build
docker compose exec openseek python -m openseek pair
docker compose exec openseek python -m openseek devices
```

* Base image `python:3.12-slim`; the process runs as UID 10001; state lives in the `/data` volume (named volume `openseek-data` in the compose file).
* The compose file publishes the owner API on the host's loopback only (`127.0.0.1:8787`). To serve devices on your LAN, change the
  published address to your host's LAN address. **Never publish it as `8787:8787` or on `0.0.0.0`, and never forward it from your router.**
  Behind Docker Desktop (Windows, macOS) and behind any reverse proxy or sidecar container, every client reaches the app from a private bridge or proxy
  address, so the built-in "private clients only" check cannot tell a LAN device from the internet (this was reproduced in the pre-release review).
  The published address is then your only access control. The server logs a warning when it detects it is running in a container. Pairing still needs
  the one-time code, but the owner API would be reachable by anyone who can reach the port.
* A TV on your LAN cannot reach `127.0.0.1:8787` on the Docker host; use the host's LAN address in the published port (for example `192.0.2.10:8787:8787`).
* The admin listener binds to the container's own loopback and is deliberately not published. Use `docker compose exec` for pairing
  and device management. The Windows desktop app needs host-loopback access to the admin port and therefore is not meant for a containerised server.
* Host networking is not used and not needed. The container needs outbound HTTPS to your media hosts.
* The image builds and starts (health endpoint, `pair` command and non-root user were checked once on Docker 29 on Windows). It has not been
  tested on other platforms or architectures, and has not been run for long periods.

## Optional: guest listener

The guest listener serves invited guest devices only; the owner API never needs internet exposure. It is off unless `OPENSEEK_PUBLIC_PORT` is set.

1. Choose a DNS name that points to your public address (a dynamic DNS service works; `backend/tools/duckdns.ps1` is a helper for one such service on Windows).
2. Set `OPENSEEK_PUBLIC_PORT=8789` and `OPENSEEK_PUBLIC_HOST=name.example.org:443` and restart. The host is used to build the URLs placed in invitations and capability links; it is never taken from a client header.
3. On your router, forward TCP 443 to port 8789 of the server only. Never forward 8787 or 8788. Give the server a fixed LAN address.
4. Turn sharing on and create an invitation (admin API `POST /admin/invites {"public": true}` or the desktop app). Send the join string or short code to the guest.
5. From another network, check with `python backend/tools/public_probe.py name.example.org:443 --pin "sha256//..."` (exit code 0 = all checks passed).

Guests are read-only by default; Contributor Mode additionally needs the owner to switch contributions on and grant the device the permission
(see `docs/SECURITY-MODEL.md`). Kill switches: sharing off (`PUT /admin/sharing {"enabled": false}`) stops every guest call at once;
`DELETE /admin/devices/<id>` revokes one device; removing the router rule closes the entrance. Per-address limits are applied on the public listener
and blocks can be lifted with `DELETE /admin/public/blocks` (loopback, with the admin secret in `X-OpenSeek-Admin`).

Admin calls go to `http://127.0.0.1:8788` and need the admin secret (see "Admin secret" above) in the `X-OpenSeek-Admin` header on every request.
The certificate is self-signed: guests authenticate the server by its pin, delivered in the invitation. `backend/tools/upnp_forward.py` is an
optional manual UPnP helper: it needs `OPENSEEK_UPNP_BASE` (your router's UPnP URL) and creates permanent port mappings, so use it only if you understand
what it opens; the router rule described above is the supported way.

## Updating

Stop the server, update the code, `pip install -r backend/requirements.txt`, start it again with the same data directory. Database changes are additive,
but back up the data directory first.
