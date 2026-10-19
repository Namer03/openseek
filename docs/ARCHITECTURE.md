# Architecture

OpenSeek is a single Python process (FastAPI under uvicorn) that supervises extraction worker subprocesses. A client sends
the URL it is playing; OpenSeek reads small ranges of that file, decodes keyframes, writes 320x180 JPEG tiles and serves them
as WebVTT cues and tile sheets. The HTTP API is specified in `api/CONTRACT-v1.md`.

## Listeners

| Listener | Default | Who | Notes |
|---|---|---|---|
| Owner API | HTTPS `0.0.0.0:8787` | paired owner devices | self-signed ECDSA P-256 certificate; client IPs must be private unless `OPENSEEK_ALLOW_PUBLIC_CLIENTS=1` |
| Admin | HTTP `127.0.0.1:8788` | the owner (CLI, dashboard, desktop app) | loopback only; rejects foreign Host/Origin; state-changing calls need `X-OpenSeek-Admin: 1` |
| Guest (public) | HTTPS, off unless `OPENSEEK_PUBLIC_PORT` is set | invited guest devices only | same certificate and pin, deny-by-default routes, per-address limits |

## Modules (`backend/openseek/`)

| Module | Role |
|---|---|
| `app.py` | Wiring, the three FastAPI apps, a pure-ASGI guard (client-IP policy, 16 KiB body limit, auth before routing, security headers), `serve()` |
| `config.py` | `OPENSEEK_*` environment settings and validation |
| `netpolicy.py` | Outbound destination policy: syntax, resolve once, every address must be global, connect to the vetted IP, header allowlist |
| `fetch.py` | Upstream client: redirects followed by OpenSeek (max 5, re-vetted, no downgrade), preflight `Range` must return 206, source fingerprint, per-job byte budget, global background token bucket, loopback range proxy |
| `mediasniff.py` | Container sniff (Matroska/WebM, MP4/MOV; anything else is `not_video`) and the Matroska index check |
| `worker.py`, `engine.py` | One worker subprocess per active source with a persistent PyAV container reading through the loopback proxy; keyframe seek, accurate re-extract when outside tolerance, HDR to SDR tone-mapping, letter/pillarboxing to 320x180, JPEG |
| `scheduler.py` | Generation order (below) |
| `jobs.py` | Jobs per device and source fingerprint, sources shared across devices, active-source cap (`429 busy`), idle reaper, supersede rule |
| `cache.py`, `db.py` | Individually stored tiles (temp file, fsync, rename; validated on read), sheets composed on demand, LRU-by-bytes and retention; SQLite for hashed tokens, device names, preview-to-source mapping and media metadata |
| `vtt.py` | WebVTT cues, clipped at neighbour midpoints; absolute URLs; layouts `tiles`, `sheets`, `hybrid` |
| `pairing.py`, `auth.py`, `tls.py` | OSP1 pairing and invitations, token hashes, capability HMAC, certificate generation and SPKI pin |
| `guard.py`, `governor.py`, `altsource.py`, `altpipeline.py` | Playback guard (client telemetry, stall detection), resource governor, experimental alternative-source negotiation |
| `library.py`, `timeline.py` | Opt-in cross-release reuse: lookup by ids, timeline mapping, offsets |
| `public.py`, `contrib.py` | Guest listener limits and blocks; Contributor Mode policy |
| `logutil.py` | Redacting log formatter |

Data directory: `openseek.db`, `secret.key`, `tls/{cert,key}.pem`, `cache/<fp[:2]>/<fp>/<profile>/{tiles,sheets}`.
On POSIX the process umask is 077.

## Request flow

1. A paired client calls `POST /v1/previews` with `source.url`, optional headers (only `User-Agent`, `Referer`, `Origin` are forwarded) and hints.
2. The server resolves and vets the destination, follows redirects itself, and checks that the host answers a range request with 206. It reads the head and tail to compute a content fingerprint
   (size plus hashes of the first and last 64 KiB), so the same file reached through a different signed link maps to the same cache entry.
3. The response (`202`) carries a capability URL for `thumbs.vtt` and a status the client polls (long-poll supported). A worker is started for the source.
4. The worker reads through `http://127.0.0.1:<port>/j/<128-bit capability>`, a loopback range proxy that applies the byte budget; the decoder never sees the real URL.
5. Tiles are published as they finish. The VTT lists only published tiles; each cue is `[pts-5 s, pts+5 s)` clipped at midpoints, so a displayed tile is always within the tolerance (default 5 s).
6. The client sends `POST /v1/previews/{id}/priority` while scrubbing and playback telemetry for the guard.

## Scheduling

Grid: one tile per 10 s. Priority classes, highest first; within a class the latest request wins:

* P0: the scrub target and its neighbours (scrub direction first), up to 6 tiles each side.
* P1: the playhead window (about 3 minutes each side).
* P2: overview, progressive: 60 s grid, then 30 s, then 10 s, ordered by distance from the playhead, throttled by `OPENSEEK_BG_MBPS` and paused for a few seconds after P0 activity.

Only one frame is in flight per source. A new scrub target replaces the queued P0 work. Optional pacing (`OPENSEEK_UPSTREAM_RPS`), multi-range prefetch
and the playback guard are off by default. The guard pauses background extraction when the client reports a rebuffer, telemetry goes stale, or a request makes no progress.

## Cache and fingerprint

Tiles are immutable once written. Tile and sheet paths are derived from validated hex ids, never from client input. Corrupt tiles (JPEG marker or size check) are dropped and regenerated.
With the opt-in title index (`metadata_index`), the database also stores IMDb/TMDB ids, season, episode and observed size per cache entry, so a lookup (`POST /v1/lookup`) can offer
another release's cached tiles. Such results are always flagged approximate and the VTT is remapped to the requesting device's timeline; the device can set an offset or reject a source.
A lookup never starts extraction.

## Roles

* Owner devices: full API.
* Guest devices (joined by invitation): lookup, their own offsets and rejections, capability reads, pairing status and leaving. With Contributor permission, also `/v1/contribute`.
  Everything else answers 403 (owner listener) or 404 (guest listener).

## Contributor Mode

Off by default. The owner enables contributions, then grants a guest the permission (invitations can carry it; it is explicit, and legacy invitations are read-only).
A contribution requires ids so the result is shared, is reused if a compatible release is cached, and otherwise extracts under `ContributionPolicy` (see `SECURITY-MODEL.md`).
Scheduling is work-conserving: when the server is idle the contribution uses the same workers and background bucket as owner jobs; while the owner is busy (extracting or playing) it
drops to one worker plus an extra `OPENSEEK_CONTRIB_MBPS` allowance; it is held entirely while the owner's playback guard is holding or the contributor's own player reports a rebuffer.
The owner always wins a source slot, and a preempted contribution resumes from its cached tiles. Limit: 6 contributions per minute per device.

## Desktop app and tools

`desktop/` is a PySide6 Windows app that attaches to a running server, reads the database read-only and uses the loopback admin API. `bench/` holds a benchmark harness with a throttled-link
test server; `backend/tools/` holds probes and helper scripts (public endpoint probe, dynamic DNS updater, contributor benchmark, multi-range probes).
