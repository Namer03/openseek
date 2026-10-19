# Pre-release security review

This is a record of one adversarial review done before the first public release. It is **not an independent professional audit.** Three
reviewers, each an AI agent that had not seen the code before, attacked isolated copies of the release candidate: (1) the API, authentication,
pairing, admin interface, TLS and information leaks; (2) outbound fetching, Contributor Mode and hostile media; (3) fresh Docker and Windows
installs, default exposure, secret storage and misconfiguration. They worked on throw-away instances on high loopback ports with synthetic data.
The maintainer then fixed the confirmed problems and added regression tests (`backend/tests/test_audit_fixes.py`).

## Confirmed or clearly demonstrated, and fixed

| Finding | Severity | Fix |
|---|---|---|
| The loopback admin API needed no secret: any local process, other local user, or container able to reach the host's loopback could pair itself as owner (reproduced end to end). GET requests needed no header at all. | Medium | Every admin call except the static dashboard page now needs a per-install secret (`<data>/admin.token`, or `OPENSEEK_ADMIN_TOKEN`) in `X-OpenSeek-Admin`. The CLI and desktop app read it; the dashboard gets it from the URL fragment (`python -m openseek dashboard`). Tests: no secret, wrong secret and the old constant header are all refused. |
| A contribution for a file the owner already had could overwrite that entry's title ids (`INSERT OR REPLACE`), relabelling the owner's cache. | Medium | Contributed jobs use first-writer-wins; the owner's own jobs can still correct a label. Tests added. |
| A slow-drip origin (one byte every few seconds) could hold an upstream read, and so one of the four shared preflight threads, for hours: the per-socket-operation timeout never fired and `read()` looped. | Medium | Reads now return after one network read and abandon a source whose total network time exceeds 30 s plus length at 16 KiB/s. Test with a real drip server. |
| `Content-Range` totals had no upper bound (an absurd total reached `fingerprint()`). | Low | Totals above 1 PiB are rejected. Tests added. |

## Hardening added without a demonstrated exploit

* The decoder is opened with FFmpeg's protocol whitelist set to nothing. The demuxer is forced and fed from a Python file object, so it should never open a protocol itself.
  Whether a hostile container could make the bundled FFmpeg read an external resource was **not tested** (see below); this closes that door regardless.
* A test pins the inbound TLS floor: the listener context has a minimum of TLS 1.2 on every supported Python.
* Start-up warnings (never failures): a Windows data directory readable by other local users, and running inside a container.
* Docker base image pinned by digest; `requirements-lock.txt` pins the whole runtime dependency closure.

## Known and accepted, documented

* Behind Docker Desktop's NAT, or any reverse proxy or sidecar, every client looks private, so the owner API's "private clients only" check cannot protect a published port (reproduced). The compose file publishes on `127.0.0.1`; the docs say never to publish `8787:8787`; the server warns in containers.
* Five wrong pairing proofs from any source burn the window, so a hostile LAN neighbour can prevent pairing (availability only, reproduced). `/v1/health` reports whether a window is open.
* On Windows the server does not set file ACLs itself; the default data directory under `%LOCALAPPDATA%` is private, others may not be.
* The owner API echoes the request's `Host` header into the capability URLs it returns to the authenticated client (the capability is not bound to the host). Behind a reverse proxy, forward a trustworthy `Host`.
* The invitation-tag oracle, non-PAKE pairing, no header-read timeout in uvicorn, and the other items in `SECURITY-MODEL.md` "Known limitations".

## Open (not fixed)

* A Contributor's cache entries share the owner's byte quota, so a contributor can cause eviction of older entries (limited to 6 contributions per minute, one source at a time). A separate contributor quota is the likely fix.
* Owner and guest preflights share one four-thread pool; contributions are not given a separate pool or a reserved-slot cap, so a stuck allowlisted origin could delay an owner preflight until the new read deadline fires.
* The cache fingerprint covers the size and the first and last 64 KiB only, so two different files with identical size, head and tail would share an entry.
* Dependencies are pinned by version, not by hash; Docker images are not rebuilt reproducibly.
* The `systemd` unit's extra options and the Linux Docker source-address behaviour have not been run.

## What was not tested

* **Denial of service and resource exhaustion under load.** The planned connection-exhaustion and slowloris tests of two reviews were stopped by a safety control before they ran and were not retried. Rate limits and the 64-connection cap are exercised only by the unit and integration tests in the suite.
* Whether a hostile MP4/MKV can steer the bundled FFmpeg to external files or the network (the reviewer was stopped before crafting such files). Mitigated by the empty protocol whitelist, not demonstrated either way.
* Contributor-mode behaviour against real debrid hosts, DNS rebinding with a live rebinding resolver (the policy resolves once and connects to the vetted address; this was reviewed in code and covered by unit tests, not attacked live), IPv6 listeners, a client on another machine, and the separate Nuvio client.
* A professional penetration test, a dependency CVE scan, and the PyInstaller build.
