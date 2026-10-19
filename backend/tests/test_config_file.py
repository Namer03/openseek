"""OPENSEEK_CONFIG_FILE: allowlisted keys only, environment wins, never crashes."""
import json
import logging

import pytest

from openseek.config import MiB, Settings


def load(tmp_path, content, env=None, raw=False):
    p = tmp_path / "openseek.json"
    if raw:
        p.write_bytes(content)
    else:
        p.write_text(json.dumps(content), encoding="utf-8")
    e = {"OPENSEEK_CONFIG_FILE": str(p)}
    e.update(env or {})
    return Settings.from_env(e)


def test_allowlisted_keys_applied(tmp_path):
    s = load(tmp_path, {"port": 9001, "admin_port": 9002, "cache_max_bytes": 512 * MiB, "retention_days": 7,
                        "bg_mbps": 4.5, "workers_per_source": 3, "playback_guard": True, "log_level": "DEBUG",
                        "contrib_hosts": "a.example", "public_port": 9003, "public_host": "x.example.org:443"})
    assert (s.port, s.admin_port, s.cache_max_bytes, s.retention_days, s.bg_mbps) == (9001, 9002, 512 * MiB, 7.0, 4.5)
    assert s.workers_per_source == 3 and s.playback_guard is True and s.log_level == "DEBUG"
    assert s.contrib_hosts == "a.example" and s.public_port == 9003 and s.public_host == "x.example.org:443"


def test_env_overrides_file(tmp_path):
    s = load(tmp_path, {"port": 9001, "cache_max_bytes": 512 * MiB}, {"OPENSEEK_PORT": "9100"})
    assert s.port == 9100 and s.cache_max_bytes == 512 * MiB


def test_forbidden_and_unknown_keys_ignored(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="openseek.config")
    d = tmp_path / "evil"
    s = load(tmp_path, {"data_dir": str(d), "capture_source_file": str(d / "x"), "allow_public_clients": True,
                        "allow_http_sources": False, "allowed_ports": "22", "admin_token": "x", "nonsense": 1,
                        "pregen": "off", "port": 9005})
    base = Settings()
    assert s.data_dir == base.data_dir and s.capture_source_file == "" and s.allow_public_clients is False
    assert s.allow_http_sources is True and s.allowed_ports == base.allowed_ports and s.admin_token == ""
    assert s.pregen == base.pregen and s.port == 9005
    assert "'data_dir'" in caplog.text and "'nonsense'" in caplog.text


@pytest.mark.parametrize("key", ["host", "public_bind"])
@pytest.mark.parametrize("val,ok", [("0.0.0.0", True), ("127.0.0.1", True), ("::1", True), ("10.1.2.3", True),
                                    ("172.16.5.5", True), ("172.31.255.1", True), ("192.168.1.20", True),
                                    ("fd12:3456::1", True), ("8.8.8.8", False), ("172.32.0.1", False),
                                    ("203.0.113.9", False), ("example.com", False), ("localhost", False),
                                    ("", False), ("2001:db8::1", False), (5, False)])
def test_private_bind_rule(tmp_path, key, val, ok):
    s = load(tmp_path, {key: val})
    default = getattr(Settings(), key)
    assert getattr(s, key) == (val if ok else default)


def test_bind_in_env_is_not_restricted(tmp_path):
    assert load(tmp_path, {}, {"OPENSEEK_HOST": "8.8.8.8"}).host == "8.8.8.8"


@pytest.mark.parametrize("content", [b"", b"not json", b"[1,2]", b"\xff\xfe\x00garbage", b'"str"', b"null", b"{" * 10])
def test_garbage_file_uses_defaults(tmp_path, content):
    assert load(tmp_path, content, raw=True) == Settings()


def test_missing_file_uses_defaults(tmp_path):
    assert Settings.from_env({"OPENSEEK_CONFIG_FILE": str(tmp_path / "nope.json")}) == Settings()
    assert Settings.from_env({"OPENSEEK_CONFIG_FILE": str(tmp_path)}) == Settings()    # a directory


def test_bom_prefixed_file(tmp_path):
    s = load(tmp_path, b"\xef\xbb\xbf" + json.dumps({"port": 9010}).encode(), raw=True)
    assert s.port == 9010


def test_bad_values_skip_only_that_key(tmp_path):
    s = load(tmp_path, {"port": "abc", "admin_port": 70000, "cache_max_bytes": 1, "bg_mbps": [1], "workers_per_source": 99,
                        "playback_guard": "maybe", "retention_days": None, "upstream_rps": True, "max_active_sources": 4})
    b = Settings()
    assert (s.port, s.admin_port, s.cache_max_bytes, s.bg_mbps, s.workers_per_source) == \
        (b.port, b.admin_port, b.cache_max_bytes, b.bg_mbps, b.workers_per_source)
    assert s.playback_guard is False and s.retention_days == b.retention_days and s.upstream_rps == b.upstream_rps
    assert s.max_active_sources == 4


def test_string_values_use_env_converters(tmp_path):
    s = load(tmp_path, {"port": "9020", "playback_guard": "yes", "bg_mbps": "2.5"})
    assert s.port == 9020 and s.playback_guard is True and s.bg_mbps == 2.5


def test_cross_field_conflict_drops_key_not_server(tmp_path):
    s = load(tmp_path, {"public_port": 8787})      # equals the default API port
    assert s.public_port == 0


def test_no_config_file_env_unchanged(tmp_path):
    assert Settings.from_env({}) == Settings()
