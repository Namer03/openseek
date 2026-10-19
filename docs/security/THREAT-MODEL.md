# OpenSeek — Threat Model

Status: draft v1 (Agent E, 2026-10-17). Scope: the OpenSeek backend (Python 3.12 + FastAPI + ffmpeg
subprocesses, Docker or native Windows, on the user's LAN) and its client integration in Nuvio Legacy
(C/SDL, NVIDIA Shield / Android TV, libcurl 8.22 + mbedTLS 3.6.7 dlopen'd per
`tools/android/deps.sh:10-11`).

Companion documents: `SECURITY-REQUIREMENTS.md` (numbered SR-x, testable), `ABUSE-TESTS.md` (QA list), `poc/` (localhost-only evidence scripts).

This threat model was written during design. Some endpoint names used here (`/v1/pair/*`, `/v1/jobs`, `/v1/sprites/*`) are draft names; the real routes are in `docs/api/CONTRACT-v1.md`. **A security
property is defined by the requirement, not by the path name.**

---

## 1. System summary and data flow

```
 Addon / AIOStreams ──stream URL + proxyHeaders──▶ Nuvio (TV) ──plays──▶ debrid CDN (TorBox…)
                                                      │
                                   (1) pinned TLS, POST body: {url, headers, duration}
                                                      ▼
                                   ┌──────────── OpenSeek backend (PC/NAS) ────────────┐
                                   │ FastAPI ──job──▶ range proxy 127.0.0.1:<eph>/j/<cap>│
                                   │                     ▲ loopback only   │ vetted fetch│
                                   │      ffmpeg ────────┘ (http,tcp only) ▼ (resolve,   │
                                   │      (sandboxed)                CDN over verified   │
                                   │         │ JPEG tiles                TLS, pinned IP)  │
                                   │         ▼                                           │
                                   │   cache/<hmac-key>/{index.vtt, NNNN.jpg}            │
                                   └─────────────────────────────────────────────────────┘
                                                      │
                                   (2) pinned TLS, Bearer token: GET VTT + sheets
                                                      ▼
                                                 Nuvio seek bar
```

The central promise is privacy: no telemetry, no central accounts, no cloud dependency, and the
maintainer never sees any activity. That promise turns into security obligations. The backend holds
the most sensitive data in the household media setup (live signed URLs, plus a record of what is being
watched). It sits on the LAN with outbound internet access, which makes it an ideal SSRF and
open-proxy pivot.

## 2. Assets

| # | Asset | Where it lives | Sensitivity | Why it matters |
|---|---|---|---|---|
| A1 | **Signed stream URL** (TorBox/debrid CDN, or AIOStreams playback URL) | TV RAM; request body; backend RAM for the job's lifetime | **High.** It is a bearer capability for hours. An AIOStreams playback URL embeds the user's *encrypted AIOStreams config* (which wraps debrid keys) and is effectively long-lived | Whoever has it can download the file on the user's account and bandwidth. It also reveals exactly what is being watched |
| A2 | **Forwarded request headers** (Referer/Origin/User-Agent; sometimes Authorization/Cookie) | Same as A1 | Medium–High | Some addons put credentials here |
| A3 | **Permanent debrid API key** (TorBox etc.) | TV only (`debrid.c`, account sync) | **Critical** | Full account takeover. **OpenSeek must never receive, need or store it** |
| A4 | **Pairing code** (8 chars, ~39.6 bits) | PC screen, backend RAM | High for its 10-minute life | Grants a device token |
| A5 | **Device token** (256-bit) | TV `openseek.txt` (app-private); backend stores SHA-256 only | High | Full API access as that device: it can submit jobs (open-proxy and SSRF attempts) and read every cached sprite |
| A6 | **Backend TLS private key** (ECDSA P-256) | Backend data dir | High | With a MITM position it lets an attacker impersonate the backend to every paired TV and harvest A1 |
| A7 | **Cached thumbnails + VTT** | Backend disk | Medium (privacy) | Images of what was watched |
| A8 | **Watch history implied by metadata**: cache keys, file mtimes/atimes, job logs, durations, per-device timestamps | Backend disk and logs | Medium (privacy) | Reconstructs viewing history without opening any image |
| A9 | **Host network position** (LAN reachability, the user's public IP, cloud metadata if hosted on a VPS) | Implicit | **High** | SSRF pivot into router admin pages, NAS, IoT, `169.254.169.254`. An open proxy makes abuse traffic come from the user's IP |
| A10 | **Host integrity** (code execution as the service user) | — | Critical | Reached via ffmpeg CVEs or a subprocess or path bug |
| A11 | **Admin capability** (create a pairing window, revoke devices, clear cache) | Loopback UI / `docker exec` CLI | High | Pairing new devices at will |

## 3. Actors

| Actor | Capabilities | Main goals |
|---|---|---|
| **T1 LAN attacker** (guest Wi-Fi client, compromised IoT device, flatmate) | Reaches every LAN port; may gain an active MITM on TV↔backend via ARP/DHCP/DNS spoofing | Harvest A1/A5, use the backend as a proxy, browse A7 |
| **T2 Malicious or compromised addon** | Chooses the stream URL and headers Nuvio forwards | SSRF into the LAN (A9), make the backend fetch arbitrary resources, feed hostile media to ffmpeg |
| **T3 Malicious media file** | Arbitrary bytes reach demuxers/decoders | Code execution (A10), resource exhaustion |
| **T4 Internet attacker** | Only if the user exposes the port (port-forward, reverse proxy, VPS) | All of T1 without needing LAN access |
| **T5 Compromised/stolen TV token** | Valid A5 | Same as an authorised device; bounded by URL policy and quotas |
| **T6 Curious maintainer / third party** | No access by design | Must learn nothing: no telemetry, no update pings, no central service |

## 4. Trust boundaries

- **B1 TV ↔ backend.** Untrusted network. Crossed only over TLS pinned to the backend's SPKI (established at pairing), with a Bearer device token. Plain HTTP carries no stream URLs, ever.
- **B2 backend ↔ internet origin.** The backend makes outbound fetches on behalf of a client; every destination is vetted after DNS resolution and on every redirect hop.
- **B3 backend ↔ ffmpeg.** ffmpeg is treated as an untrusted parser: it never sees the real URL, only a loopback range-proxy capability; protocol and format allowlists, resource limits, killed on timeout.
- **B4 backend ↔ disk.** Only derived thumbnails and opaque keys are written; path components are generated, never client-supplied.
- **B5 admin ↔ backend.** Pairing windows and revocation are local actions (loopback UI or CLI inside the container), never available to a LAN client.

## 5. STRIDE summary

| Threat | Example | Primary mitigation (requirement) |
|---|---|---|
| Spoofing | Rogue backend on LAN impersonates OpenSeek to harvest stream URLs | SPKI pin bound into pairing proof (SR-2, SR-3) |
| Spoofing | LAN client guesses a pairing code | 10-min, single-use, 5-attempt codes; global attempt rate limit (SR-1) |
| Tampering | Client poisons the cache for another release | Cache key is server-derived content fingerprint (SR-14) |
| Repudiation | — (single-household tool; not a goal) | Minimal non-identifying event log (SR-20) |
| Information disclosure | Stream URL in logs, query strings, crash dumps | POST body only; redaction; no URL persistence (SR-4, SR-19, SR-20) |
| Information disclosure | Unauthenticated sprite access | Bearer or short-lived capability required for every data URL (SR-6, SR-7) |
| Information disclosure | Viewing history from disk metadata | No titles/URLs stored; opaque keys; retention limit (SR-15, SR-16) |
| Denial of service | Job floods, huge durations, slow clients | Bounded queue, per-device quotas, byte budgets, timeouts (SR-11, SR-12) |
| Elevation of privilege | SSRF to router/NAS/metadata | Post-resolution destination policy, backend-owned redirects, loopback proxy for ffmpeg (SR-8, SR-9, SR-10) |
| Elevation of privilege | ffmpeg memory-safety bug via hostile media | Sandbox: non-root, read-only rootfs, cap_drop, no-new-privileges, limits; format allowlist (SR-13, SR-17) |

## 6. Residual risks (accepted for v1, documented to users)

- Pairing is password-authenticated key confirmation, not a true PAKE (see SECURITY-REQUIREMENTS SR-2 rationale). Upgrade path: CPace.
- ffmpeg remains a large native attack surface; containment, not elimination.
- A compromised paired TV can make the backend fetch any *public* URL within quotas.
- Users who port-forward the service accept internet exposure; the backend refuses non-private client source IPs by default.
- Privacy design does not remove copyright or other legal risk of the user's own media sources.
