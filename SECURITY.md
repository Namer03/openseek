# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately using GitHub Security Advisories ("Report a vulnerability" under the
repository's Security tab). Do not open a public issue or pull request for an undisclosed vulnerability.

Include the affected version or commit, how to reproduce it, the impact you believe it has, and whether you intend to publish. Do not include real
media links, tokens or invitation codes; use synthetic ones.

## Scope

In scope: the server in `backend/openseek/` (authentication and pairing, capability URLs, the guest and admin listeners, the outbound fetch policy and
SSRF protections, Contributor Mode policy, logging and redaction, the cache), the Docker files, and the desktop app in `desktop/`.

Out of scope: vulnerabilities in third-party dependencies that are not reachable through OpenSeek (report them upstream), attacks that need
control of the host or of the data directory, denial of service by a device the owner has already authorised within its quotas, a deployment that
deliberately forwards the owner API to the internet, and the legal status of media a user processes.

## Supported versions

Only the latest released version (currently 0.1.x) receives security fixes.

## What to expect

This is a volunteer-maintained project. We aim to acknowledge reports within a reasonable time, to keep you informed while we investigate, and to
credit reporters who want credit once a fix is available. We cannot promise specific response or fix times.

See `docs/SECURITY-MODEL.md` for the intended security properties and known limitations.
