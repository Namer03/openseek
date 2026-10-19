#!/bin/sh
# Let devices on your home network (an Android TV) reach the OpenSeek container, without exposing it any wider.
#
# Detects your private LAN address, publishes the owner API on THAT address only (never 0.0.0.0), starts the container, then verifies
# with `docker inspect` that the published binding is exactly that address; if not, the container is stopped again. Public, CGNAT,
# link-local and loopback addresses are refused. Nothing else on your computer or router is changed.
#
#   tools/docker-lan.sh                    detect the LAN address, publish on it, start
#   tools/docker-lan.sh --ip 192.168.1.20  use this address (one of your own private addresses)
#   tools/docker-lan.sh --port 18787       use a different host port (default 8787)
#   tools/docker-lan.sh --off              back to this computer only (127.0.0.1)
set -eu
cd "$(dirname "$0")/.."
IP=""; PORT=8787; OFF=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --off) OFF=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

is_private() {
  echo "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || return 1
  echo "$1" | grep -Eq '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' || return 1
  for o in $(echo "$1" | tr . ' '); do [ "$o" -le 255 ] || return 1; done
  return 0
}

if [ "$OFF" = 1 ]; then IP=127.0.0.1
elif [ -z "$IP" ]; then
  if command -v ip >/dev/null 2>&1; then
    IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')
  elif command -v ipconfig >/dev/null 2>&1; then
    IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
  fi
  [ -n "$IP" ] || { echo "No LAN address found. Pass one with --ip (for example 192.168.1.20)." >&2; exit 1; }
fi
if [ "$IP" != 127.0.0.1 ] && ! is_private "$IP"; then
  echo "$IP is not a private LAN address (10.x, 172.16-31.x, 192.168.x). The owner API must never be published on it." >&2; exit 1
fi
case "$PORT" in ''|*[!0-9]*) echo "--port must be a number" >&2; exit 2 ;; esac
[ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || { echo "--port must be 1024..65535" >&2; exit 2; }

printf 'OPENSEEK_LAN_IP=%s\nOPENSEEK_HOST_PORT=%s\n' "$IP" "$PORT" > .env
docker compose up -d --build

# verify what Docker really published (fail closed)
ID=$(docker compose ps -q openseek)
BOUND=$(docker inspect --format '{{range $p,$b := .NetworkSettings.Ports}}{{range $b}}{{.HostIp}} {{end}}{{end}}' "$ID")
OK=1; [ -n "$BOUND" ] || OK=0
for b in $BOUND; do [ "$b" = "$IP" ] || OK=0; done
if [ "$OK" != 1 ]; then
  docker compose down
  echo "Docker published the port on [$BOUND] instead of only $IP. The container was stopped." >&2; exit 1
fi

if [ "$IP" = 127.0.0.1 ]; then
  echo "OpenSeek is reachable from this computer only: https://127.0.0.1:$PORT"
else
  echo "OpenSeek is published on your LAN address only: https://$IP:$PORT"
  echo "Pair a device:   docker compose exec openseek python -m openseek pair"
  echo "On the TV enter the server address $IP (port $PORT) and the code shown. Do not forward this port from your router."
fi
