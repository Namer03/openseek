# OpenSeek — Automated Abuse Tests

Implemented in `backend/tests/test_security_*.py`. Each test names the requirement it proves.
Every test MUST run against the real app (ASGI or live uvicorn) — mocks only for the *remote origin*,
which is a local test server.

## 1. Authentication & pairing
| Test | Input | Expected | SR |
|---|---|---|---|
| test_every_route_requires_auth | every route in OpenAPI except health/pair, no token | 401 | 6 |
| test_bad_token_401 | random 256-bit token | 401, constant-time path | 5,6 |
| test_revoked_token_401 | revoke device then call | 401 | 5 |
| test_pair_requires_open_window | claim with no admin window | 403/409 | 1 |
| test_pair_code_single_use | correct claim twice | 2nd fails | 1 |
| test_pair_code_expiry | claim after TTL (clock injected) | fails | 1 |
| test_pair_attempt_cap | 5 wrong proofs then correct | window burned | 1 |
| test_pair_replay_rejected | replay a captured claim body | rejected | 2 |
| test_pair_wrong_pin_rejected | proof computed over another SPKI | rejected | 2 |
| test_pair_proof_vectors | fixed vectors shared with Nuvio C tests | match | 2 |
| test_token_hash_only_on_disk | grep data dir for token | absent | 5 |

## 2. Capability URLs
| test_cap_expired_403 | cap with past exp | 403 | 7 |
| test_cap_other_job_403 | cap for job A used on job B | 403 | 7 |
| test_cap_tampered_403 | flip one hex digit | 403 | 7 |

## 3. SSRF matrix (submitted as job source; all MUST fail with `source_forbidden`, and the loopback canary server MUST record zero hits)
`http://127.0.0.1/`, `http://127.1/`, `http://[::1]/`, `http://0x7f000001/`, `http://2130706433/`, `http://017700000001/`,
`http://localhost./`, `http://[::ffff:127.0.0.1]/`, `http://169.254.169.254/latest/`, `http://[fe80::1]/`, `http://10.0.0.1/`,
`http://192.168.1.1/`, `http://100.64.0.1/`, `http://[fd00::1]/`, `http://0.0.0.0/`, `http://user:pass@example.com/`,
`file:///etc/passwd`, `concat:a|b`, `subfile:,,start,0,end,1,,:x`, `data:video/mp4;base64,AA`, `gopher://x/`, `ftp://x/`,
a public-looking hostname whose resolver (injected) returns 127.0.0.1, port 22.
Plus:
| test_redirect_to_loopback_blocked | public origin (resolver-injected) 302 → http://127.0.0.1:<canary> | blocked, 0 canary hits | 9 |
| test_redirect_chain_limit | 6 hops | `too_many_redirects` | 9 |
| test_redirect_drops_headers_cross_origin | Referer forwarded, cross-origin hop | header absent at hop 2 | 9 |
| test_hls_playlist_rejected | origin serves m3u8 referencing canary | rejected, 0 canary hits | 10 |
| test_proxy_loopback_only | connect to range proxy on LAN IP | refused | 10 |
| test_proxy_unknown_cap_404 | random cap | 404 | 10 |
| test_no_shell_true | grep source for shell=True / os.system | none | 10 |

## 4. Resource exhaustion
| test_job_flood_429 | queue+1 jobs | 429 | 12 |
| test_concurrency_bound | N jobs | ≤ workers ffmpeg processes alive | 12 |
| test_byte_budget_enforced | budget 1 MiB | job fails `budget_exceeded`, server bytes ≤ budget + 1 chunk | 11 |
| test_no_range_rejected | origin ignores Range | `range_unsupported`, bytes ≤ probe window | 11 |
| test_oversize_body_413 | 1 MiB JSON | 413 | 11 |
| test_header_allowlist | Cookie/Authorization/X-Foo forwarded | absent at origin | 11 |
| test_duration_cap | duration_ms 10^12 | 422 | 12 |

## 5. Storage & privacy
| test_no_url_on_disk | unique marker in URL path+query | absent from data dir and logs | 15,20 |
| test_path_traversal_ids | `../`, `%2e%2e`, uppercase, overlong IDs in every path param | 404/422, no file outside data dir | 16 |
| test_corrupt_sheet_regenerated | truncate a cached JPEG | detected, regenerated or 404 not 500 | 16 |
| test_lru_eviction | cap below cache size | least-recently-used evicted | 16 |
| test_logs_redacted | capture logs during job | no marker, no token, no cap | 20 |
| test_security_headers | any response | nosniff, no-store/private | 19 |
| test_public_client_ip_refused | X-Forwarded-For ignored; client 8.8.8.8 (injected) | 403 | 17 |
