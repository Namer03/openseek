# OpenSeek — Security Requirements

Each requirement is testable. "Verify" names the automated test or review that proves it
(see `ABUSE-TESTS.md`). MUST = release blocker. SHOULD = fix or document before v1.

## Pairing and transport

**SR-1 (MUST) Pairing codes.** 8 symbols from a 31-symbol unambiguous alphabet (~39.6 bits), generated with
`secrets`. Valid for at most 10 minutes, single use, invalidated after 5 wrong attempts. At most one
pairing window open at a time, opened only by a local admin action (B5). Global rate limit on pair claims
(≤ 10/min). *Verify:* `test_pair_code_single_use`, `test_pair_code_expiry`, `test_pair_attempt_cap`,
`test_pair_requires_open_window`.

**SR-2 (MUST) Pairing protocol OSP1** (reference: `poc/pairing_reference.py`).
1. On first start the backend generates an ECDSA P-256 key and a self-signed certificate (0600). Pin =
   `sha256//base64(SHA-256(SPKI DER))`.
2. `POST /v1/pair/start` (TLS, not yet pinned) → `{pair_id, salt, spki_pin, iterations}`. No secret.
3. TV derives `K = PBKDF2-HMAC-SHA256(code, "OSP1"‖pair_id‖salt‖spki_pin, iterations)`; refuses iterations < 200 000.
4. `POST /v1/pair/claim` **over TLS pinned to spki_pin** with `client_proof = HMAC(K, "client"‖pair_id‖device_name)`.
5. Server verifies in constant time, returns `{device_id, token, server_proof = HMAC(K, "server"‖pair_id‖SHA-256(token))}`.
6. TV stores token + pin **only** if server_proof verifies.
*Rationale:* a MITM that substitutes its own key causes the honest TV to compute K over the attacker's pin;
the attacker can only recover the code by offline guessing at ~2^39.6 × PBKDF2 cost within the code's 10-minute
life — impractical — and a wrong guess burns the window. Not a PAKE; upgrade path CPace.
*Verify:* `test_pair_proof_vectors`, `test_pair_wrong_pin_rejected`, `test_pair_replay_rejected`.

**SR-3 (MUST) Pinned TLS for every TV↔backend request after pairing.** The client sets
`CURLOPT_PINNEDPUBLICKEY` (fails closed if the option cannot be set). The backend serves HTTPS only on its
API port. *Verify:* Nuvio `tests/openseek*.sh` (pin option applied, refusal when unavailable), `test_api_no_plain_http`.

**SR-4 (MUST) Stream URLs and headers travel only in a POST JSON body over pinned TLS.** Never in a query
string or path. If pinning is unavailable on the device, the client does not send them (no plain-HTTP fallback).
*Verify:* Nuvio code review + `test_jobs_reject_url_in_query`.

**SR-5 (MUST) Device tokens.** 256-bit random; server stores only SHA-256(token); constant-time comparison;
revocable per device by local admin. *Verify:* `test_token_hash_only_on_disk`, `test_revoked_token_401`.

## Authorisation

**SR-6 (MUST) Every endpoint except `GET /v1/health` and the two pairing endpoints requires a valid Bearer token**
(401 otherwise, no detail). *Verify:* `test_every_route_requires_auth` (enumerates the OpenAPI schema).

**SR-7 (MUST) Data URLs (VTT, sheets)** use capability paths `/v1/c/{cap}/...` where `cap` = HMAC(server key,
device_id‖job_id‖exp), expiring in ≤ 6 h and bound to the job. They are only ever served over the pinned TLS port.
Bearer is also accepted. *Verify:* `test_cap_expired_403`, `test_cap_other_job_403`, `test_cap_tampered_403`.

## Outbound fetching (SSRF / open proxy)

**SR-8 (MUST) Destination policy after resolution.** Schemes: `https` (and `http` only for public destinations,
since many debrid CDNs are http). Ports: 80, 443, 8080, 8443 by default (configurable). Every resolved address
must be global unicast: deny loopback, private (RFC1918), CGNAT 100.64/10, link-local, ULA fc00::/7, multicast,
reserved, unspecified, IPv4-mapped/NAT64/6to4 that embed denied v4, and the cloud metadata addresses. No userinfo
in URLs. Connect to the vetted IP (no second resolution). *Verify:* SSRF matrix in `ABUSE-TESTS.md` §3.

**SR-9 (MUST) Redirects are followed by the backend, not ffmpeg.** At most 5 hops; each re-checked by SR-8;
https→http downgrade refused; forwarded headers dropped when origin changes. *Verify:* `test_redirect_to_loopback_blocked`,
`test_redirect_chain_limit`, `test_redirect_drops_headers_cross_origin`.

**SR-10 (MUST) ffmpeg isolation from the network.** ffmpeg only reads `http://127.0.0.1:<port>/j/<128-bit cap>`
served by the backend's range proxy, with `-protocol_whitelist http,tcp`, a forced `-f` from the backend's own sniff
(`matroska` or `mov`/`mp4`), `-format_whitelist matroska,mov,mp4`, bounded `-probesize`/`-analyzeduration`. The proxy
listens on loopback only and rejects unknown caps. No `shell=True` anywhere. *Verify:* `test_proxy_loopback_only`,
`test_proxy_unknown_cap_404`, `test_hls_playlist_rejected`, `test_no_shell_true` (static grep).

**SR-11 (MUST) Byte and time budgets.** Per job: maximum bytes fetched (default 4 GiB — full 10 s coverage of a 3 h film measured at ~1–2 MB/frame
needs ~1–2 GB), background byte-rate cap (default 8 Mbit/s), wall-clock limit,
per-frame ffmpeg timeout with process-tree kill. Forwarded header allowlist: `User-Agent`, `Referer`, `Origin`
(others dropped). Request bodies ≤ 16 KiB. Content must answer a Range request with 206; servers without Range
support are rejected (no full download). *Verify:* `test_byte_budget_enforced`, `test_no_range_rejected`,
`test_header_allowlist`, `test_oversize_body_413`.

**SR-12 (MUST) Resource bounds.** Bounded job queue (reject with 429 when full), bounded worker concurrency,
per-device active-job limit, max duration accepted (e.g. 12 h), max decoded frame size (`-max_pixels`), job
deduplication. *Verify:* `test_job_flood_429`, `test_concurrency_bound`.

**SR-13 (SHOULD) Media sanity.** Reject sources whose probed dimensions exceed 8192×8192 or that expose no video stream.

## Storage and privacy

**SR-14 (MUST) Cache key is server-derived:** `SHA-256("v1"‖size‖SHA-256(first 64 KiB)‖SHA-256(last 64 KiB))`,
computed from bytes the backend fetched itself. Client hints are never trusted as keys.

**SR-15 (MUST) Never persisted:** stream URLs, forwarded headers, titles, IMDb IDs, pairing codes, raw tokens.
Persisted: thumbnails, VTT, cache key, duration, sheet geometry, creation/last-use time, hashed device tokens.
*Verify:* `test_no_url_on_disk` (scan data dir for the test URL and host after a job).

**SR-16 (MUST) Filesystem hygiene.** Data dir 0700, files 0600 (where the OS supports it); atomic writes
(temp file + fsync + rename); all path components derived from validated lowercase hex IDs; corrupted entries
are detected (size/JPEG marker check) and regenerated. Configurable cache size cap with LRU eviction and retention.
*Verify:* `test_path_traversal_ids`, `test_corrupt_sheet_regenerated`, `test_lru_eviction`.

## Deployment, logging, privacy

**SR-17 (MUST) Container baseline.** Non-root UID, read-only rootfs, tmpfs for temp, `cap_drop: [ALL]`,
`no-new-privileges`, default seccomp, memory/CPU/pids limits, no host network, no UPnP. Publish on the LAN only;
the backend refuses client source IPs that are not private/loopback unless `OPENSEEK_ALLOW_PUBLIC_CLIENTS=1`.

**SR-18 (MUST) No telemetry.** No outbound connections other than client-requested media fetches. No update
checks by default. *Verify:* review + `test_no_outbound_on_idle`.

**SR-19 (MUST) No secrets in responses.** Errors are stable codes, never echo URLs or headers. Response headers:
`Cache-Control: no-store` on API JSON, `private` on data; `X-Content-Type-Options: nosniff`.

**SR-20 (MUST) Log redaction.** Logs never contain query strings, userinfo, tokens, pairing codes or forwarded
headers; hosts are reduced to a short salted hash. Uvicorn access logging of paths containing caps is redacted.
*Verify:* `test_logs_redacted` (run a job with a unique marker in the URL; grep all log output).

**SR-21 (SHOULD) Dependencies** pinned with hashes; `pip-audit` run in CI; base image digest pinned.
