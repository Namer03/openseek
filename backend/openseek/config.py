"""Runtime configuration from OPENSEEK_* environment variables.

Every setting is documented in the README (configuration table). There is deliberately NO environment
switch that relaxes the outbound destination policy (SR-8): the only way to let the
fetcher reach a loopback test origin is to construct `netpolicy.TestOnlyAllowance` in
Python code (tests do that), which production entry points never do.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

GiB = 1 << 30
MiB = 1 << 20

PREGEN_MODES = ("off", "overview", "full")


class ConfigError(ValueError):
    pass


def _bool(v: str) -> bool:
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError(f"not a boolean: {v!r}")


def _ports(v: str) -> frozenset[int]:
    out = set()
    for part in v.split(","):
        part = part.strip()
        if not part:
            continue
        p = int(part)
        if not 1 <= p <= 65535:
            raise ConfigError(f"bad port {p}")
        out.add(p)
    if not out:
        raise ConfigError("OPENSEEK_ALLOWED_PORTS is empty")
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8787
    admin_port: int = 8788                 # always bound to 127.0.0.1 (contract)
    cache_max_bytes: int = 2 * GiB
    retention_days: float = 30.0
    bg_mbps: float = 8.0                   # P2 (background) token bucket, Mbit/s
    upstream_rps: float = 0.0              # opt-in global request pacing; 0 disables
    job_bytes: int = 4 * GiB               # per-job upstream byte budget
    tolerance_s: float = 5.0
    pregen: str = "full"                   # off | overview | full
    p2_pause_s: float = 5.0
    p1_interleave: int = 0                 # 0 preserves strict P1 priority
    nearest_keyframe: bool = False         # also accept the keyframe just after a slot (cuts forward decoding)
    alt_source: bool = False               # low-resolution thumbnail source (OPENSEEK_ALT_SOURCE); off by default
    alt_mbps: float = 64.0                 # byte allowance for extraction from the alternative source
    alt_rps: float = 12.0                  # request pacing for the alternative source
    alt_shared_caps: bool = False          # ONE request bucket and ONE byte bucket for playback source + alternative
    alt_hold_after_reject: bool = False    # after a rejected candidate wait for a release before offering the next
    alt_report_names: bool = False         # release names in the loopback diagnostics (never persisted by the server)
    alt_skip_names: str = ""               # comma-separated release-name fragments never offered (experiments)
    admin_token: str = ""                  # secret the loopback admin API requires (empty: a random one in <data>/admin.token)
    capture_source_file: str = ""          # diagnostics: write the playback source's resolved CDN link here after preflight
    multirange_prefetch: bool = False      # EXPERIMENTAL: fetch several keyframe windows per request (multipart/byteranges)
    multirange_batch: int = 8              # windows per multi-range request (2..16)
    playback_guard: bool = False           # pause background extraction on rebuffer / stale telemetry / CDN stall
    guard_stall_s: float = 6.0             # a request without a first byte (or a body that stopped) this long = CDN stall
    guard_time_scale: float = 1.0          # scales every guard/telemetry timer (tests only)
    alt_dry_run: bool = False              # negotiate and report eligible candidates, but never offer/resolve one
    alt_max_resolutions_5min: int = 6      # ceiling on alt link resolutions (each may cost one provider API call)
    alt_probe_conc: int = 3                # parallel probe workers during validation (latency-bound)
    alt_min_gain_s: float = 8.0            # only negotiate/validate when the projected saving is at least this
    alt_validation_est_s: float = 8.0      # prior for validation time (replaced by measured values)
    alt_max_conc: int = 4                  # hard ceiling on concurrent alt range requests (<= workers_per_source)
    hierarchical: bool = False             # coarse-to-fine full-timeline scheduling (60s -> 30s -> 10s)
    allowed_ports: frozenset[int] = field(default_factory=lambda: frozenset({80, 443, 8080, 8443}))
    allow_http_sources: bool = True        # http only ever reaches *public* destinations
    allow_public_clients: bool = False     # SR-17
    contrib_mbps: float = 16.0             # extra byte cap for guest-contributed extraction ONLY while the owner is busy (idle: same as the owner)
    contrib_max_sources: int = 1           # guest-contributed sources running at once (one slot always stays free for the owner)
    contrib_hosts: str = "torbox.app,tb-cdn.io,real-debrid.com,debrid.link,alldebrid.com,premiumize.me"   # default allowlist (the owner can edit it)
    contrib_https_only: bool = True        # tests only
    public_port: int = 0                   # the internet-facing GUEST listener (0 = not started); see public.py
    public_bind: str = "0.0.0.0"
    public_host: str = ""                  # advertised in invitations / borrowed URLs: "name.duckdns.org:443"
    max_active_sources: int = 2            # global cap on sources with a live worker (429 busy)
    workers_per_source: int = 1            # bounded parallel extraction workers per active source (1..4)
    max_jobs_per_device: int = 1           # older active job of that device is stopped
    max_jobs_total: int = 256              # job records kept in memory
    idle_timeout_s: float = 600.0          # worker killed after 10 min without client activity
    frame_timeout_s: float = 30.0          # hard per-frame timeout (accurate mode gets 2x)
    open_timeout_s: float = 45.0           # worker open/probe timeout
    create_wait_s: float = 4.0             # POST /v1/previews waits this long for preflight
    max_duration_s: float = 12 * 3600.0    # SR-12
    max_dimension: int = 8192              # SR-13
    upstream_timeout_s: float = 15.0
    log_level: str = "INFO"

    @property
    def tolerance_ms(self) -> int:
        return int(round(self.tolerance_s * 1000))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        s = cls()
        conv = {
            "DATA_DIR": ("data_dir", Path),
            "HOST": ("host", str),
            "PORT": ("port", int),
            "ADMIN_PORT": ("admin_port", int),
            "CACHE_MAX_BYTES": ("cache_max_bytes", int),
            "RETENTION_DAYS": ("retention_days", float),
            "BG_MBPS": ("bg_mbps", float),
            "UPSTREAM_RPS": ("upstream_rps", float),
            "JOB_BYTES": ("job_bytes", int),
            "TOLERANCE_S": ("tolerance_s", float),
            "PREGEN": ("pregen", str),
            "P2_PAUSE_S": ("p2_pause_s", float),
            "P1_INTERLEAVE": ("p1_interleave", int),
            "HIERARCHICAL": ("hierarchical", _bool),
            "ALT_SOURCE": ("alt_source", _bool),
            "ALT_MBPS": ("alt_mbps", float),
            "ALT_RPS": ("alt_rps", float),
            "ALT_MAX_CONC": ("alt_max_conc", int),
            "ALT_MIN_GAIN_S": ("alt_min_gain_s", float),
            "ALT_PROBE_CONC": ("alt_probe_conc", int),
            "ALT_DRY_RUN": ("alt_dry_run", _bool),
            "PLAYBACK_GUARD": ("playback_guard", _bool),
            "GUARD_STALL_S": ("guard_stall_s", float),
            "GUARD_TIME_SCALE": ("guard_time_scale", float),
            "MULTIRANGE_PREFETCH": ("multirange_prefetch", _bool),
            "MULTIRANGE_BATCH": ("multirange_batch", int),
            "CAPTURE_SOURCE_FILE": ("capture_source_file", str),
            "ADMIN_TOKEN": ("admin_token", str),
            "ALT_SKIP_NAMES": ("alt_skip_names", str),
            "ALT_SHARED_CAPS": ("alt_shared_caps", _bool),
            "ALT_HOLD_AFTER_REJECT": ("alt_hold_after_reject", _bool),
            "ALT_REPORT_NAMES": ("alt_report_names", _bool),
            "ALT_MAX_RESOLUTIONS_5MIN": ("alt_max_resolutions_5min", int),
            "ALT_VALIDATION_EST_S": ("alt_validation_est_s", float),
            "NEAREST_KEYFRAME": ("nearest_keyframe", _bool),
            "ALLOWED_PORTS": ("allowed_ports", _ports),
            "ALLOW_HTTP_SOURCES": ("allow_http_sources", _bool),
            "ALLOW_PUBLIC_CLIENTS": ("allow_public_clients", _bool),
            "PUBLIC_PORT": ("public_port", int),
            "CONTRIB_MBPS": ("contrib_mbps", float),
            "CONTRIB_MAX_SOURCES": ("contrib_max_sources", int),
            "CONTRIB_HOSTS": ("contrib_hosts", str),
            "PUBLIC_BIND": ("public_bind", str),
            "PUBLIC_HOST": ("public_host", str),
            "MAX_ACTIVE_SOURCES": ("max_active_sources", int),
            "WORKERS_PER_SOURCE": ("workers_per_source", int),
            "MAX_JOBS_PER_DEVICE": ("max_jobs_per_device", int),
            "IDLE_TIMEOUT_S": ("idle_timeout_s", float),
            "FRAME_TIMEOUT_S": ("frame_timeout_s", float),
            "CREATE_WAIT_S": ("create_wait_s", float),
            "LOG_LEVEL": ("log_level", str),
        }
        kw = {}
        for suffix, (name, fn) in conv.items():
            raw = env.get("OPENSEEK_" + suffix)
            if raw is None or raw == "":
                continue
            try:
                kw[name] = fn(raw)
            except (ValueError, ConfigError) as e:
                raise ConfigError(f"OPENSEEK_{suffix}: {e}") from None
        s = replace(s, **kw)
        if s.allow_public_clients and env.get("OPENSEEK_ALLOW_PUBLIC_CLIENTS_ACK") != "owner-api-is-public":
            raise ConfigError("OPENSEEK_ALLOW_PUBLIC_CLIENTS=1 turns off the owner API's client-address check. Remote access is what the guest "
                              "listener is for; if you really mean it, also set OPENSEEK_ALLOW_PUBLIC_CLIENTS_ACK=owner-api-is-public")
        s.validate()
        return s

    def validate(self) -> None:
        if not 0 <= self.p2_pause_s <= 60 or not 0 <= self.p1_interleave <= 100:
            raise ConfigError("P2_PAUSE_S must be 0..60; P1_INTERLEAVE must be 0..100")
        if self.pregen not in PREGEN_MODES:
            raise ConfigError("OPENSEEK_PREGEN must be off|overview|full")
        if not 0 < self.tolerance_s <= 5.0:
            # the 10 s grid only guarantees non-overlapping cues for tolerance <= 5 s
            raise ConfigError("OPENSEEK_TOLERANCE_S must be in (0, 5]")
        if self.bg_mbps <= 0:
            raise ConfigError("OPENSEEK_BG_MBPS must be > 0")
        if not 1.0 <= self.guard_stall_s <= 60.0 or not 0.01 <= self.guard_time_scale <= 1.0:
            raise ConfigError("OPENSEEK_GUARD_STALL_S must be 1..60")
        if not 2 <= self.multirange_batch <= 16:
            raise ConfigError("OPENSEEK_MULTIRANGE_BATCH must be 2..16")
        if not 0 <= self.upstream_rps <= 100:
            raise ConfigError("OPENSEEK_UPSTREAM_RPS must be 0..100")
        if self.job_bytes < 1 * MiB:
            raise ConfigError("OPENSEEK_JOB_BYTES must be >= 1 MiB")
        if self.max_active_sources < 1 or self.max_jobs_per_device < 1:
            raise ConfigError("source/job caps must be >= 1")
        if not 1 <= self.workers_per_source <= 4:
            raise ConfigError("OPENSEEK_WORKERS_PER_SOURCE must be 1..4")
        if self.cache_max_bytes < 16 * MiB:
            raise ConfigError("OPENSEEK_CACHE_MAX_BYTES must be >= 16 MiB")
        for p in (self.port, self.admin_port, self.public_port):
            if not 0 <= p <= 65535:
                raise ConfigError("bad port")
        if not 0.5 <= self.contrib_mbps <= 1000 or not 1 <= self.contrib_max_sources <= 4:
            raise ConfigError("OPENSEEK_CONTRIB_MBPS must be 0.5..1000 and CONTRIB_MAX_SOURCES 1..4")
        if self.public_port and self.public_port in (self.port, self.admin_port):
            raise ConfigError("OPENSEEK_PUBLIC_PORT must differ from the API and admin ports")
        if self.public_host and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?:\d{1,5}", self.public_host):
            raise ConfigError("OPENSEEK_PUBLIC_HOST must be host:port, e.g. name.duckdns.org:443")

    def with_(self, **kw) -> "Settings":
        s = replace(self, **kw)
        s.validate()
        return s

