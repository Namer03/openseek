# Security model

OpenSeek holds sensitive material: live media links (bearer capabilities for their lifetime) and a record of what is being watched. It also makes outbound
requests on behalf of clients, which makes it a potential SSRF and open-proxy pivot. The design goal is a small, auditable attack surface with no telemetry.

This document describes intended behaviour and what the automated tests check (`backend/tests/`). It is not an independent audit. Protocol vectors for pairing are in `backend/tests/vectors/osp1.json`;
reference scripts are in `docs/security/poc/`.

## Trust boundaries

1. Client to server: untrusted network. TLS only; the client pins the server's SPKI; a Bearer device token is required.
2. Server to media host: the server fetches URLs chosen by a client. Every destination is vetted after DNS resolution and on every redirect hop.
3. Server to decoder: the decoder (PyAV/ffmpeg libraries) parses untrusted media in a separate worker process started with an argument list (no shell), a minimal environment, below-normal priority, and
   access only to a loopback proxy capability, not the real URL. There is no OS-level sandbox; use container or OS limits.
4. Server to disk: only derived thumbnails and opaque ids are written; path components are generated from validated hex ids.
5. Admin: loopback only, never reachable from the LAN or the internet.

## Listeners and exposure

* Owner API (8787): HTTPS. By default it refuses client addresses that are not private or loopback (`X-Forwarded-For` is not trusted). Do not forward it from your router.
* Admin (8788): bound to 127.0.0.1; foreign Host or Origin headers are rejected (DNS rebinding) and state-changing calls need `X-OpenSeek-Admin: 1`.
* Guest listener (off by default): exact method+path allow-list, everything else a uniform 404. Owner tokens are refused on it. Per-address limits (per minute: health 30, pairing 12,
  guest API 300, capability URLs 900, unknown routes 30); 20 refused requests in 10 minutes block an address for 15 minutes. Capability URLs are built from `OPENSEEK_PUBLIC_HOST`, not from client headers.
  It shares a process with the extractor, so a flood could slow it; the listener caps concurrency at 64 connections.
* Plain HTTP is not served on the API ports: a plain-HTTP request to a TLS port gets no content and no redirect. Tests confirm TLS 1.2 and TLS 1.3 handshakes with an AEAD cipher suite.
  Refusal of TLS 1.0/1.1 relies on the defaults of the Python and OpenSSL build in use (uvicorn is not configured with an explicit minimum) and is not tested here.

## Authentication

* Pairing (OSP1): the owner opens a window (10 minutes, single use, burned after 5 wrong proofs, at most one window at a time). The code has 8 symbols from a 31-symbol alphabet (about 39.6 bits).
  The client derives `K = PBKDF2-HMAC-SHA256(code, "OSP1" || pair_id || salt || spki_pin, iterations)` (the server default is 600,000; `MIN_CLIENT_ITERATIONS = 200000` is the floor clients are expected to enforce, which this repository does not itself check) and sends an HMAC proof over a TLS connection pinned to that key; the server proves
  knowledge of `K` in return. A substituted certificate changes the pin and therefore the key. This is key confirmation with a low-entropy secret, not a PAKE; an active man-in-the-middle can try offline guesses within the window.
* Device tokens: 256-bit random; only the SHA-256 is stored; constant-time comparison; revocable per device.
* Capabilities: `/v1/c/<cap>/...` where `cap` is an HMAC over device, preview and expiry (at most 6 hours), served only over TLS.
* Invitations (guests): created locally, expiring (10 minutes to 7 days), single use by default, stored as the derived key, burned after 5 wrong proofs, revocable. They cannot be redeemed while sharing is off.
  Typed codes bind the pin into the derivation, so a server that does not hold the invited key cannot produce a valid proof.
* Every route except health and the pairing endpoints requires a token; a test enumerates the OpenAPI routes. The guest permission list is an explicit allow-list enforced before routing.

## Outbound fetching (SSRF policy)

* Schemes http (public destinations only, can be disabled) and https; default ports 80, 443, 8080, 8443; no userinfo; no other schemes.
* DNS is resolved once; every address must be global unicast. Denied: loopback, private, CGNAT, link-local, unique-local, multicast, reserved, unspecified, IPv4-mapped/NAT64/6to4 forms embedding those, and metadata addresses.
  The connection goes to the vetted IP, with SNI and certificate checks against the host name.
* Redirects are followed by OpenSeek only: at most 5, each re-vetted, no https-to-http downgrade, forwarded headers dropped when the origin changes.
* The source must answer range requests with 206; otherwise it is rejected rather than downloaded. Per-job byte budget, per-frame timeouts and a bounded number of active sources apply.
* Only `User-Agent`, `Referer` and `Origin` are forwarded. Request bodies are limited to 16 KiB.
* No environment variable relaxes this policy. Tests that reach loopback origins construct an explicit test-only allowance in code.

## Contributor safety

Contributions are off until the owner enables them and grants a device the permission. Every hop of a contributed fetch passes `ContributionPolicy` before the standard policy:
https only, port 443, a host on the owner's allowlist (default: common debrid and CDN hosts, editable), no userinfo; the allowlisted name must also resolve to public addresses.
A refusal returns a fixed `source_not_allowed` and never says why; the owner sees refused host names only. The link is held in memory only and is not written to the database, logs or metrics
(the one exception is the owner-enabled diagnostic option `OPENSEEK_CAPTURE_SOURCE_FILE`, described below).
Contributors cannot use `POST /v1/previews`, priority, telemetry or alternative-source routes. Rate limit: 6 contributions per minute per device. Revoking the device, removing the permission,
switching contributions off, switching sharing off or deleting the contribution stops the job immediately.

## What is stored, logged and not logged

Stored: thumbnails, VTT inputs, cache fingerprints, durations, timestamps, SHA-256 of device tokens, device names, invitation keys (derived keys, not codes), and, only with the opt-in title index, IMDb/TMDB ids, season, episode and file size.
The TLS private key and the capability HMAC key are in the data directory (files 0600 where the OS supports it).

Not stored by default: source URLs, forwarded headers, titles, file names, pairing codes, raw tokens, or the contributor's link. Tests (`test_logs_and_disk_redacted` and the contribution tests) scan the data directory
and the logs for a unique marker placed in a source URL. Exception: if the owner sets `OPENSEEK_CAPTURE_SOURCE_FILE` (empty by default, intended for diagnostics only), the resolved link of each job is written in clear text
to that file with owner-only permissions; leave it unset in normal use.

Logging: log lines never contain query strings, userinfo, tokens, capability segments, pairing codes or forwarded headers; host names are reduced to a salted hash that changes on every restart. The uvicorn access log is enabled and records
client IP, method, redacted path and status. OpenSeek does not rotate or truncate logs; rotate them yourself if viewing history is a concern. The start-up banner prints the bind address, LAN URL and certificate pin.
There is no telemetry, update check or other outbound connection except media fetches requested by clients (a test checks that an idle server makes none). OpenSeek Desktop contacts a public metadata service (Cinemeta) for titles and posters.

## Known limitations

* Pairing is not a PAKE (see above); the upgrade path would be CPace.
* The decoder libraries remain a large native attack surface; containment is process separation and limits, not elimination.
* A compromised paired device can make the server fetch any public URL within quotas.
* The guest listener cannot distinguish clients behind one NAT; blocks apply per address. A guest can learn whether a title is cached by looking it up. A live 4-character invitation tag is distinguishable from an unknown one.
* Uvicorn has no header-read timeout; slow connections hold a slot until the OS drops them.
* An observer on the network path to a guest sees DNS lookups, the TLS server name, IP addresses, timing and volume, but not paths, ids, tokens, VTT or JPEG contents.
* Dependencies are pinned by version, not hash.
* Technical privacy is separate from the legal status of the media you process; see `RESPONSIBLE-USE.md`.

## Verification status

Checked against the code and the automated tests (455 backend tests passing, 4 skipped, on a clean checkout): the guest-listener limits and block timings, pairing parameters (window, 5-proof burn, alphabet, 256-bit tokens,
constant-time comparisons), capability lifetime (at most 6 hours), invitation lifetimes (10 minutes to 7 days), the SSRF policy constants (ports, 5 redirects, 16 KiB bodies, header allow-list), the contribution rate (6 per
minute per device), the worker environment and priority, and the "no outbound connection when idle" and "logs and disk are redacted" tests.

Not verified, or verified only in part:

* Refusal of TLS 1.0/1.1 (relies on the Python/OpenSSL defaults; only 1.2 and 1.3 acceptance is tested).
* The pairing iteration floor for clients: enforced by the client, not by this repository. The client-protocol tests (`test_nuvio_client_protocol.py` and one contribution test) need a checkout of the separate Nuvio Legacy
  client, which is not part of this repository, and are skipped without it.
* The documents under `docs/security/` (threat model, requirements, abuse tests, contribution design) were written during development. They state requirements and intent. Items marked "Verify" in them may point at
  tests of that external client or at tests renamed since; treat them as design notes, not as proof.
* The `systemd` unit and its hardening options in `docs/INSTALL.md`, the Docker image on platforms other than the one it was built on, and long-running behaviour of the container.
* Per-OS file permissions: owner-only modes apply where the operating system supports them (not on typical Windows setups).
* There has been no independent security audit. Dependencies are pinned by version, not by hash.
