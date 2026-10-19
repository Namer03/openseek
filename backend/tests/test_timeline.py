"""Timeline validator: accepts affine-compatible releases, rejects everything else (synthetic films)."""
from __future__ import annotations

import numpy as np
import pytest

from openseek import timeline as tl

DUR = 7_200_000


class Film:
    """Deterministic 'film': shots of 3-15 s, each a smooth random picture that drifts while it plays."""

    def __init__(self, seed: int, dur_ms: int = DUR):
        r = np.random.default_rng(seed)
        self.dur = dur_ms
        bounds, t = [0], 0.0
        while t < dur_ms / 1000:
            t += float(r.uniform(3, 15)); bounds.append(t)
        self.bounds = np.array(bounds) * 1000
        self.seed = seed

    def frame(self, t_ms: float) -> np.ndarray:
        i = int(np.searchsorted(self.bounds, t_ms, side="right")) - 1
        r = np.random.default_rng(self.seed * 100003 + i)
        base = np.kron(r.uniform(30, 225, (9, 16)), np.ones((10, 10)))          # 160x90
        drift = int((t_ms - self.bounds[i]) / 1000 * 1)   # ~1 px/s pan: slow camera move
        return np.roll(base, drift, axis=1).astype(np.uint8)


def sampler(film: Film, to_film_time, gop_ms: int, noise_seed: int, dur_ms: int):
    r = np.random.default_rng(noise_seed)

    def grab(t_ms: int):
        pts = int(t_ms // gop_ms * gop_ms)                                      # keyframe snapping
        f = film.frame(to_film_time(pts)).astype(np.float32) + r.normal(0, 3, (90, 160))
        return pts, tl.signature(np.clip(f, 0, 255).astype(np.uint8))
    return grab


def exact_sampler(film: Film, to_film_time, noise_seed: int):
    """Frame at exactly the requested time (accurate decode): what the production refinement stage uses."""
    r = np.random.default_rng(noise_seed)

    def grab(t_ms: int):
        f = film.frame(to_film_time(t_ms)).astype(np.float32) + r.normal(0, 3, (90, 160))
        return int(t_ms), tl.signature(np.clip(f, 0, 255).astype(np.uint8))
    return grab


def verdict(film_a, film_b, map_b_to_film, dur_b, seed=0, gop_b=4000):
    """Production configuration: real keyframe lists + exact-decode refinement."""
    ga = sampler(film_a, lambda t: t, 2000, seed + 1, DUR)
    gb = sampler(film_b, map_b_to_film, gop_b, seed + 2, dur_b)
    kfa, kfb = list(range(0, DUR, 2000)), list(range(0, dur_b, gop_b))
    return tl.verify_alignment(DUR, dur_b, ga, gb, kfa, kfb, exact_sampler(film_b, map_b_to_film, seed + 3))


def _assert_mapping_accurate(v, truth, seed):
    """The accepted mapping must predict B's time for A's time to within the keyframe-noise bound."""
    assert v.accepted, v.reason
    for t in np.random.default_rng(seed).uniform(60_000, DUR - 60_000, 40):
        assert abs(v.mapping.to_b(t) - truth(t)) <= tl.MAX_RESIDUAL_MS
    assert v.mapping.tolerance_for_b() >= 1000                        # still usable for keyframe extraction


@pytest.mark.parametrize("seed", range(10))
def test_identity_offset_and_speed_are_accepted_with_accurate_mapping(seed):
    f = Film(seed)
    _assert_mapping_accurate(verdict(f, f, lambda t: t, DUR, seed), lambda t: t, seed)
    _assert_mapping_accurate(verdict(f, f, lambda t: t - 7000, DUR + 7000, seed), lambda t: t + 7000, seed)
    v = verdict(f, f, lambda t: t * 25 / 24, int(DUR * 24 / 25), seed)       # PAL speed-up
    _assert_mapping_accurate(v, lambda t: t * 24 / 25, seed)


def test_different_film_is_never_accepted():
    accepted = sum(verdict(Film(s), Film(s + 1000), lambda t: t, DUR, s).accepted for s in range(1000))
    assert accepted == 0                                                  # false-acceptance: 0/1000


@pytest.mark.parametrize("seed", range(10))
def test_inserted_scene_and_extended_cut_are_rejected(seed):
    f = Film(seed)
    ins = 120_000                                                         # 2 min of extra footage mid-film
    mid = DUR // 2
    v = verdict(f, f, lambda t: t if t < mid else max(0, t - ins), DUR + ins, seed)
    assert not v.accepted and v.reason in ("nonlinear_timeline", "no_visual_match")


def test_duration_gate_and_flat_frames():
    assert tl.metadata_gate(DUR, int(DUR * 1.5)) == "duration_mismatch"
    assert tl.signature(np.full((90, 160), 10, np.uint8)) is None        # black frame carries no information
    v = tl.verify_alignment(DUR, DUR, lambda t: (t, None), lambda t: (t, None))
    assert not v.accepted


def test_mapping_helpers_keep_the_5s_contract():
    m = tl.Mapping(a=0.96, b=0.0, max_residual_ms=2000, inliers=8, kind="speed", fit_err_ms=800)
    assert m.tolerance_for_b() == 3700
    assert tl.slot_ok(m, 100_000, int(100_000 * 0.96) + 2000)
    assert not tl.slot_ok(m, 100_000, int(100_000 * 0.96) + 9000)
