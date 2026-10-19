# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased]

### Licence
* OpenSeek is released under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later); `LICENSE` added and `pyproject.toml` updated. Source code and the Dockerfile are published; prebuilt Docker images are not, because the FFmpeg libraries inside the PyAV wheels are a GPL build.

### Security
* The loopback admin API now requires a per-install secret (`<data>/admin.token` or `OPENSEEK_ADMIN_TOKEN`) on every call except the static dashboard page; new `python -m openseek dashboard`. Previously any local process could pair itself as owner.
* Contributions can no longer overwrite the title ids of an existing cache entry.
* Upstream reads abandon slow-drip sources (wall-clock throughput floor); absurd `Content-Range` totals are rejected.
* The decoder is opened with an empty FFmpeg protocol whitelist; the inbound TLS 1.2 floor is pinned by a test.
* Start-up warnings for a Windows data directory readable by other local users and for running in a container.
* Docker base image pinned by digest; `requirements-lock.txt` pins the full runtime dependency closure.
* Fail-closed deployment rules: a container refuses to start unless `OPENSEEK_PUBLISHED_ON` is loopback or a private LAN address; owner and admin requests carrying proxy headers are refused; `OPENSEEK_ALLOW_PUBLIC_CLIENTS` needs an explicit acknowledgement; Windows refuses a data directory other users can write to.
* Supported Docker LAN setup: `tools/docker-lan.ps1` / `tools/docker-lan.sh` publish the owner API on a private LAN address only and verify the binding. The compose image is now `openseek-selfhosted:local`.
* Upgrade note: the desktop app and any client that sent `X-OpenSeek-Admin: 1` must be updated to the matching version (see `docs/INSTALL.md` "Updating"). Pairings, devices, sharing and invitations carry over unchanged.
* See `docs/security/PRE-RELEASE-REVIEW.md` for the review, the remaining open items and what could not be tested.

## [0.1.0] - Initial public release

### Added
* Seek-preview service: WebVTT plus 320x180 JPEG tiles and 3200x1800 sheets (`tiles`, `sheets` and `hybrid` layouts), generated on demand from HTTP(S) sources with bounded range reads.
* Scheduler: scrub target first (latest wins), playhead window, progressive overview; background byte cap; tolerance of 5 s.
* Persistent per-source worker subprocess (PyAV) behind a loopback range proxy; HDR to SDR tone-mapping.
* Optional experiments, off by default: multi-range prefetch, global request pacing, alternative low-resolution source negotiation, hierarchical scheduling.
* Playback guard: pauses background extraction on rebuffer, stale telemetry or source stall.
* Thumbnail cache with LRU eviction and retention; opt-in title index for cross-release reuse with approximate timeline mapping, offsets and rejections.
* Device pairing (OSP1) with certificate pinning, SHA-256 token storage, expiring capability URLs, CLI (`serve`, `pair`, `devices`, `revoke`, `openapi`).
* Admin interface on loopback with Host/Origin checks and a CSRF header.
* Optional invite-only public guest listener with deny-by-default routing, per-address limits and temporary blocks.
* Contributor Mode: allowlisted, https-only, SSRF-safe contributions with a work-conserving scheduler that yields to the owner, contributor playback telemetry, and preemption with resume.
* SSRF-hardened outbound fetching, log redaction, no telemetry.
* OpenSeek Desktop (PySide6, Windows): dashboard, poster library, live activity, logs, sharing, settings.
* Dockerfile and compose file; benchmark harness (`bench/`); probes and helper scripts (`backend/tools/`).
* Documentation: architecture, security model, installation, API contract, responsible use, licence options.

### Known limitations
See the README section "Status and limitations".
