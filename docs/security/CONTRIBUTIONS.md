# Guest contributions (Reader / Contributor)

**Default:** off. Guests stay read-only unless the owner (1) turns on *Allow guest contributions* (admin API `PUT /admin/contributions`, Desktop > Sharing)
**and** (2) grants that device the Contributor permission (`PUT /admin/devices/{id}/permissions {"contribute":true}`). Existing guests are Readers.

**API (guest, public or LAN listener):** `POST /v1/contribute`, `GET /v1/contribute/{id}`, `DELETE /v1/contribute/{id}`. The privileged
`POST /v1/previews`, priority, telemetry and alt-source routes stay closed to guests (public listener: 404, LAN listener: 403).
`GET /v1/pair/status` reports `contribute: true|false` for the calling device (the effective permission).

**Request:** `source{url, headers (User-Agent/Referer/Origin only)}`, `imdb_id`/`tmdb_id`, `season`, `episode`, `duration_ms` (ids are mandatory so the result is
shared). A compatible cached release is reused first (answer `200 {"state":"reused",...}`, no extraction). Otherwise `202` with the same body as a preview
(state, progress, capability `vtt_url`); thumbnails appear progressively and for everybody once the entry is in the library.

**Safety model**
* Every hop of the fetch (initial URL and each redirect) passes `ContributionPolicy`: https only, port 443, host must be on the owner's allowlist
  (default: torbox.app, tb-cdn.io, real-debrid.com, debrid.link, alldebrid.com, premiumize.me; editable), no userinfo; then the standard SSRF policy
  (no private/loopback/link-local/metadata addresses; DNS resolved once and pinned). A refusal never says why (`source_not_allowed`); the owner sees refused HOST NAMES only.
* WORK-CONSERVING scheduling. One guest source at a time globally (`OPENSEEK_CONTRIB_MAX_SOURCES`) and the global limits stay in force. When the server is idle a
  contribution gets the SAME worker count (`OPENSEEK_WORKERS_PER_SOURCE`) and draws from the SAME 64 Mbit/s background bucket as the owner's own extraction
  (so the total can never exceed it). While the owner is busy (extracting, or playing: fresh playback telemetry on an owner source) it steps back to ONE worker
  and an extra `OPENSEEK_CONTRIB_MBPS` floor (default 16 Mbit/s). It is held completely while the owner's playback guard is holding (rebuffer, stale telemetry, CDN stall)
  or the owner is serving an urgent scrub, and while the CONTRIBUTOR's own player reports a rebuffer (`POST /v1/contribute/{id}/telemetry`, sent by the Nuvio client
  about every 1.5 s). The owner always wins a source slot: the contribution is paused and resumes from its cached tiles on the contributor's next status poll.
* 6 contributions per minute per device; the public listener's per-address limits still apply.
* The link lives in memory only. It is never written to the database, logs or metrics (logs carry a salted hash of the host).
* Revoke a device, remove its permission, switch contributions off, switch sharing off, or `DELETE /admin/contributions/{preview_id}`: the job stops immediately.
* If the host cannot reach the stream, the job fails with a fixed error code; no credentials are ever requested from the guest.
* Use only for content the owner and the guest have the right to process and share.

Tests: `backend/tests/test_contributions.py` (Reader refused, Contributor end to end through the real client code, reuse without extraction, unsafe links,
privilege escalation, limits, owner preemption, revocation, kill switch, no logging of the link, owner-hold logic).

## Benchmark (same real HTTPS source, isolated server with production settings)

Source: Big Buck Bunny 720p from archive.org (62 MB, HTTPS, redirects to an `*.us.archive.org` host, 60 thumbnails). Alternating runs, cold cache each time
(`tools/bench_contrib.py`; settings = tools/run_production.ps1: 2 workers, 64 Mbit/s, 3 requests/s, multi-range, guard on).

| run | wall time | thumbnails/min | MB read | CDN requests |
|---|---|---|---|---|
| owner | 24.9 s | 144.6 | 36.0 | 86 |
| contributor | 20.6 s | 175.0 | 36.2 | 88 |
| contributor | 21.9 s | 164.5 | 39.2 | 90 |
| owner | 26.5 s | 136.0 | 37.2 | 88 |

Medians: owner 25.7 s, contributor 21.3 s. Same work (same bytes, same requests); the idle contributor is NOT slower, it was about 17 % faster in all 4 alternating runs.
Likely reason (not isolated): an owner job starts with a playhead-centred priority pass, a contribution just walks the timeline. Not measured here: a contribution
running while the owner plays (it deliberately slows down then), and a slow real debrid CDN.
