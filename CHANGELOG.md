# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased]

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
