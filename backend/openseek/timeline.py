"""Timeline compatibility between the playback source (A) and a lower-resolution thumbnail source (B).

Pure logic plus two injected frame samplers; no network, no cache writes. A candidate is accepted
only when sampled frames spread across the whole film agree with one affine mapping
`t_B = a * t_A + b`. Anything else (different cut, inserted scenes, drifting audio-sync edits) is
rejected: the safe answer is "use the original source".

Sampler contract: `grab(t_ms) -> (actual_pts_ms, signature)` where `signature` comes from `signature()`.
Matching always uses the ACTUAL presentation time of the decoded frames, never the requested time.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

SIG_W, SIG_H = 32, 18
# spread across the whole film; golden-ratio jitter so anchors do not all share one phase against B's keyframe grid
ANCHOR_FRACTIONS = tuple((i + 0.2 + 0.6 * ((i * 0.6180339887) % 1.0)) / 20 for i in range(20))
MIN_CORR = 0.60              # same picture, allowing keyframe-snapping and compression differences
MIN_MARGIN = 0.05            # ... and clearly better than the runner-up (repetitive scenes are ambiguous)
MIN_INLIERS = 8              # of 20 anchors (keyframe snapping and shot cuts spoil some)
MAX_RESIDUAL_MS = 8000       # pairing residual allowed: the nearest keyframes of A and B can be ~(gopA+gopB)/2 apart
MAX_FIT_ERR_MS = 3500        # reject if the mapping itself is not certain to this level (leaves >= 1 s tolerance)
MAX_RATIO_ERR = 0.10         # |a - 1| limit (PAL speed-up is 4.2 %)
WIDE_WINDOW_MS = 12_000      # search window around the prediction for the first anchor
WIDE_STEP_MS = 3_000
NARROW_WINDOW_MS = 3_000     # once a prediction exists
NARROW_STEP_MS = 1_000
MIN_FLAT_STD = 4.0           # near-uniform frames (black, logos fade) carry no information
MIN_PER_QUARTER = 2          # agreeing anchors needed in EACH quarter of the film (rejects mid-film inserts)
SPEED_MIN = 0.01             # |duration ratio - 1| treated as a real speed change
SHOT_MS = 8_000              # candidates this far from the best are treated as a different shot


@dataclass(frozen=True)
class Mapping:
    a: float                 # t_B = a * t_A + b   (milliseconds)
    b: float
    max_residual_ms: float   # largest pairing residual: keyframe sparsity, NOT the mapping error
    inliers: int
    kind: str                # "identity" | "offset" | "speed" | "affine"
    fit_err_ms: float = 0.0  # statistical bound on |to_b(t) - truth| over the whole film (see _fit_error)

    def to_b(self, t_a_ms: float) -> float:
        return self.a * t_a_ms + self.b

    def to_a(self, t_b_ms: float) -> float:
        return (t_b_ms - self.b) / self.a

    def tolerance_for_b(self, total_ms: int = 5000, margin_ms: int = 500) -> int:
        """Keyframe tolerance to use when extracting from B so the end-to-end error vs the slot stays
        within `total_ms` even after the mapping residual and a safety margin."""
        return max(0, int(total_ms - self.fit_err_ms - margin_ms))


@dataclass
class Verdict:
    accepted: bool
    reason: str
    mapping: Mapping | None = None
    anchors: int = 0
    samples_a: int = 0
    samples_b: int = 0
    detail: str = ""        # numbers behind a rejection (fit error, residual) for diagnostics


@dataclass
class Sampler:
    """One independent set of frame grabbers (own decoders/connections). A pool of samplers lets independent
    probes run in parallel, which matters because validation is bound by round-trip latency, not bandwidth."""
    grab_a: Callable[[int], tuple[int, "np.ndarray | None"]]
    grab_b: Callable[[int], tuple[int, "np.ndarray | None"]]
    grab_b_exact: "Callable[[int], tuple[int, np.ndarray | None]] | None" = None
    cache_b: dict = field(default_factory=dict)
    calls_a: int = 0
    calls_b: int = 0


def _run_pool(samplers: "list[Sampler]", tasks: list):
    """Run callables `task(sampler)` over the pool; results keep task order. Exceptions propagate."""
    if len(samplers) <= 1 or len(tasks) <= 1:
        return [t(samplers[0]) for t in tasks]
    import queue
    from concurrent.futures import ThreadPoolExecutor

    free: "queue.Queue[Sampler]" = queue.Queue()
    for sm in samplers:
        free.put(sm)

    def go(task):
        sm = free.get()
        try:
            return task(sm)
        finally:
            free.put(sm)
    with ThreadPoolExecutor(max_workers=len(samplers)) as ex:
        return list(ex.map(go, tasks))


class Sig(np.ndarray):
    """A signature vector that remembers the frame's luma contrast (std, 0-255) for diagnostics."""
    contrast: float = 0.0


def signature(jpeg_or_gray, ) -> np.ndarray | None:
    """32x18 mean-removed, unit-norm grayscale vector from JPEG bytes (or a 2-D uint8 array).
    The outer 10 % (16 % top and bottom: letterbox bars, burnt-in strips) is ignored (letterbox bars differ between releases). None if the frame is flat."""
    from PIL import Image

    if isinstance(jpeg_or_gray, (bytes, bytearray)):
        img = Image.open(io.BytesIO(jpeg_or_gray)).convert("L")
    else:
        img = Image.fromarray(np.asarray(jpeg_or_gray, dtype=np.uint8), "L")
    w, h = img.size
    dx, dy = int(w * 0.1), int(h * 0.16)
    img = img.crop((dx, dy, w - dx, h - dy)).resize((SIG_W, SIG_H), Image.BILINEAR)
    v = np.asarray(img, dtype=np.float32).ravel()
    std = float(v.std())
    if std < MIN_FLAT_STD:
        return None
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    if n <= 0:
        return None
    out = (v / n).view(Sig)
    out.contrast = std
    return out


def correlation(x: np.ndarray, y: np.ndarray) -> float:
    return float(np.dot(x, y))


def fit_affine(pairs: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Least-squares t_B = a t_A + b over (t_A, t_B); returns (a, b, max |residual|)."""
    ta = np.array([p[0] for p in pairs], dtype=np.float64)
    tb = np.array([p[1] for p in pairs], dtype=np.float64)
    a, b = np.polyfit(ta, tb, 1)
    return float(a), float(b), float(np.max(np.abs(tb - (a * ta + b))))


def _median_gap(kfs: list[int] | None) -> float:
    return float(np.median(np.diff(kfs))) if kfs is not None and len(kfs) > 2 else 4000.0


def _fit_error(inl: list[tuple[float, float]], a: float, b: float, dur_a_ms: int,
               gap_a_ms: float = 4000.0, gap_b_ms: float = 4000.0) -> float:
    """Upper bound (about 3 sigma + margin) on the mapping error, kept apart from keyframe-pairing noise.

    Pairing residuals are large when keyframes are sparse but are unbiased (jittered anchors, nearest
    keyframe on either side), so the offset error shrinks as sigma / sqrt(n). A residual trend over time
    means the fixed-slope assumption is wrong and is added in full at the far end of the film.
    Quantisation: if both keyframe grids happen to be phase-locked, nearest-keyframe pairing is biased
    in the same direction for every anchor and averaging cannot remove it; allow half the finer grid
    spacing (empirical bound, validated in simulation over several GOP combinations, not a proof; the
    post-generation spot check is what catches a violation)."""
    pa = np.array([p[0] for p in inl], dtype=np.float64)
    r = np.array([p[1] - (a * p[0] + b) for p in inl], dtype=np.float64)
    sigma = float(np.std(r))
    trend = abs(float(np.polyfit(pa, r, 1)[0])) * dur_a_ms / 2 if len(inl) >= 3 else 0.0
    quant = 0.5 * min(gap_a_ms, gap_b_ms)
    return 3.0 * sigma / float(np.sqrt(len(inl))) + trend + quant + 250.0


REFINE_ANCHORS = 8
REFINE_OFFSETS_MS = (-2000, -1000, 0, 1000, 2000)
MIN_REFINED = 5
REFINE_FLOOR_MS = 500            # sub-sample resolution of the refined pairing (parabolic peak of a smooth score)


def _refine(inl: list[tuple[float, float]], a: float, b: float, sig_at_a: dict, samplers: "list[Sampler]"
            ) -> list[tuple[float, float]]:
    """Near-simultaneous pairs: for evenly spread anchors, decode B at exact times around the coarse
    prediction and take the parabolic peak of the similarity, so keyframe spacing no longer limits the offset.
    Anchors are independent, so they run across the sampler pool."""
    ordered = sorted(inl)
    step = max(1, len(ordered) // REFINE_ANCHORS)
    anchors = [pa for pa, _ in ordered[::step][:REFINE_ANCHORS] if sig_at_a.get(pa) is not None]

    def one(pa: float):
        def task(sm: Sampler):
            sa = sig_at_a[pa]
            pts: list[float] = []
            cs: list[float] = []
            for d in REFINE_OFFSETS_MS:
                pb, sb = sm.grab_b_exact(int(max(0, a * pa + b + d)))
                sm.calls_b += 1
                if sb is not None:
                    pts.append(float(pb))
                    cs.append(correlation(sa, sb))
            if len(cs) < 3:
                return None
            k = int(np.argmax(cs))
            if cs[k] < MIN_CORR:
                return None
            t = pts[k]
            if 0 < k < len(cs) - 1 and pts[k + 1] > pts[k - 1]:      # parabola through the peak and its neighbours
                x = np.array(pts[k - 1:k + 2]) - pts[k]
                y = np.array(cs[k - 1:k + 2])
                den = y[0] - 2 * y[1] + y[2]
                if den < -1e-9:
                    t = pts[k] + float(np.clip(0.5 * (y[0] - y[2]) / den, -1, 1)) * float(np.mean(np.abs(x)))
            return (pa, t)
        return task
    return [r for r in _run_pool(samplers, [one(pa) for pa in anchors]) if r is not None]


def _refined_error(pairs: list[tuple[float, float]], a: float, b: float, dur_a_ms: int) -> float:
    """3 sigma / sqrt(n) on the offset + two standard errors of an unmodelled slope at the far end of the
    film + the pairing resolution floor. No quantisation term: pairs are near-simultaneous."""
    pa = np.array([p[0] for p in pairs])
    r = np.array([p[1] - (a * p[0] + b) for p in pairs])
    n = len(pairs)
    sigma = float(np.std(r))
    spread = float(np.std(pa)) or 1.0
    slope_bound = 2.0 * (sigma / (np.sqrt(n) * spread)) * dur_a_ms / 2
    return 3.0 * sigma / float(np.sqrt(n)) + slope_bound + REFINE_FLOOR_MS


def _classify(a: float, b: float) -> str:
    if abs(a - 1.0) < 5e-4:
        return "identity" if abs(b) < 500 else "offset"
    return "speed" if abs(b) < 2000 else "affine"


def metadata_gate(dur_a_ms: int, dur_b_ms: int) -> str | None:
    """Cheap reject before any sampling. None = continue."""
    if dur_a_ms <= 0 or dur_b_ms <= 0:
        return "no_duration"
    if abs(dur_b_ms / dur_a_ms - 1.0) > MAX_RATIO_ERR:
        return "duration_mismatch"
    return None


def _slopes(ratio: float) -> tuple[float, ...]:
    """Speed hypotheses. Same speed always; the duration ratio too when it could be a real speed change
    (PAL 24 -> 25 fps is 4 %). On a short film a few seconds of extra intro look like a few percent, so the
    ratio is never trusted by itself: callers pick between the hypotheses by how well each explains the matched
    frames (`_best_hypothesis`). Smaller differences are extra or missing material, not drift."""
    return (1.0, ratio) if abs(ratio - 1.0) >= SPEED_MIN else (1.0,)


def _hypotheses(matches: list[tuple[float, float]], ratio: float) -> list[tuple[float, float, float]]:
    """[(a, b, rms)] for each speed hypothesis, best (smallest RMS pairing residual) first; ties keep same speed
    first. Residuals are measured against the hypothesis' own median offset."""
    out = []
    for a in _slopes(ratio):
        bs = np.array([pb - a * pa for pa, pb in matches])
        b = float(np.median(bs))
        out.append((a, b, float(np.sqrt(np.mean((bs - b) ** 2)))))
    return sorted(out, key=lambda h: h[2])


DECISIVE = 0.7                    # coarse RMS ratio at which one hypothesis clearly beats the other


def _best_hypothesis(matches: list[tuple[float, float]], ratio: float) -> tuple[float, float] | None:
    h = _hypotheses(matches, ratio)
    return (h[0][0], h[0][1]) if h else None


def _decisive(matches: list[tuple[float, float]], ratio: float) -> bool:
    h = _hypotheses(matches, ratio)
    return len(h) < 2 or h[0][2] < DECISIVE * h[1][2]


def _constrained_fit(pairs: list[tuple[float, float]], ratio: float) -> tuple[float, float, float]:
    """Simplest mapping that explains the matches: one of the speed hypotheses with its own offset (fewer free
    parameters average the keyframe noise better than a free two-parameter fit). `inf` residual = none fits."""
    ab = _best_hypothesis(pairs, ratio)
    if ab is not None:
        a, b = ab
        res = max(abs(pb - (a * pa + b)) for pa, pb in pairs)
        if res <= MAX_RESIDUAL_MS:
            return a, b, res
    return _slopes(ratio)[0], 0.0, float('inf')   # no simple mapping explains the matches


def _anchor_order(n: int) -> list[int]:
    """Visit anchors in a spread-out order so early ones already span the film."""
    step = next(k for k in (7, 9, 11, 13, 3) if np.gcd(k, n) == 1)
    return [(i * step) % n for i in range(n)]


def _kf_window(kfs: list[int], centre: float, window: float) -> list[int]:
    """Real keyframes in [centre-window, centre+window] plus the ones bracketing `centre`, so the nearest
    keyframe on EITHER side is always a candidate (requesting 'time t' alone only ever yields keyframes <= t)."""
    import bisect

    lo, hi = bisect.bisect_left(kfs, centre - window), bisect.bisect_right(kfs, centre + window)
    i = bisect.bisect_left(kfs, centre)
    pick = set(kfs[lo:hi]) | set(kfs[max(0, i - 1):i + 1])
    return sorted(pick)


def verify_alignment(dur_a_ms: int, dur_b_ms: int, grab_a: Callable[[int], tuple[int, np.ndarray | None]] | None = None,
                     grab_b: Callable[[int], tuple[int, np.ndarray | None]] | None = None,
                     kfs_a: list[int] | None = None, kfs_b: list[int] | None = None,
                     grab_b_exact: Callable[[int], tuple[int, np.ndarray | None]] | None = None,
                     slot_ms: int | None = None, samplers: "list[Sampler] | None" = None) -> Verdict:
    """Probe anchors across the film, let matches vote for one offset, fit one affine mapping, or reject.

    Matches pair frames that are the nearest keyframes of A and B, not simultaneous instants, so a
    correct pairing can disagree by about half a keyframe interval; `max_residual_ms` records that
    and later shrinks the keyframe tolerance used when extracting from B. With a pool of `samplers`, independent
    anchors are probed in parallel batches (each batch uses the offset voted by the previous ones)."""
    why = metadata_gate(dur_a_ms, dur_b_ms)
    if why:
        return Verdict(False, why)
    if samplers is None:
        samplers = [Sampler(grab_a, grab_b, grab_b_exact)]
    P = len(samplers)
    ratio = dur_b_ms / dur_a_ms
    n = len(ANCHOR_FRACTIONS)
    matches: list[tuple[float, float]] = []
    sig_at_a: dict[float, np.ndarray] = {}
    diag: list[tuple] = []        # per anchor: (outcome, best corr, runner-up corr, A time ms, best offset ms, A contrast)

    def vote_offset() -> tuple[float, float] | None:
        """Best (a, b) over the matches so far, from the hypothesis that explains them best."""
        if len(matches) < 2:
            return None
        return _best_hypothesis(matches, ratio)

    def probe(idx: int, ab):
        def task(sm: Sampler):
            t_a = int(dur_a_ms * ANCHOR_FRACTIONS[idx])
            if slot_ms:                                 # anchor on a tile slot: the frame doubles as a finished tile
                t_a = min(int(round(t_a / slot_ms)) * slot_ms, max(0, dur_a_ms - 1))
            if kfs_a:
                t_a = min(_kf_window(kfs_a, t_a, 0), key=lambda k: abs(k - t_a))     # snap to a real keyframe
            pts_a, sig_a = sm.grab_a(t_a)
            sm.calls_a += 1
            if sig_a is None:
                return ("x", "flat", None, None, float(pts_a), None, 0.0)   # flat frame: skip, do not guess
            if ab is None:
                centre, window, step = ratio * pts_a, WIDE_WINDOW_MS, WIDE_STEP_MS
            else:
                centre, window, step = ab[0] * pts_a + ab[1], NARROW_WINDOW_MS, NARROW_STEP_MS
            if kfs_b:
                times = _kf_window(kfs_b, centre, window)
            else:
                times, t = [], centre - window
                while t <= centre + window:
                    times.append(int(t))
                    t += step
            cands: list[tuple[float, int]] = []
            seen: set[int] = set()
            for t in times:
                t = max(0, min(int(t), dur_b_ms - 1))
                if t not in sm.cache_b:
                    sm.cache_b[t] = sm.grab_b(t)
                    sm.calls_b += 1
                pts_b, sig_b = sm.cache_b[t]
                if sig_b is not None and pts_b not in seen:
                    seen.add(pts_b)
                    cands.append((correlation(sig_a, sig_b), pts_b))
            if not cands:
                return ("x", "nocand", None, None, float(pts_a), None, float(getattr(sig_a, "contrast", 0.0)))
            corr, pts_b = max(cands)
            # runner-up only counts away from the best: neighbours inside one shot legitimately look alike
            far = [c for c, p in cands if abs(p - pts_b) > SHOT_MS]
            second = max(far) if far else -1.0
            if corr >= MIN_CORR and corr - second >= MIN_MARGIN:
                return (float(pts_a), float(pts_b), sig_a, corr, second, float(pts_b - pts_a), float(getattr(sig_a, 'contrast', 0.0)))
            return ("x", "weak" if corr < MIN_CORR else "ambiguous", corr, second, float(pts_a), float(pts_b - pts_a),
                    float(getattr(sig_a, "contrast", 0.0)))
        return task

    order = _anchor_order(n)
    pos = 0
    while pos < len(order):
        # the first batch has no prior (wide search); later batches narrow around the voted offset
        batch = order[pos:pos + (P if vote_offset() is not None or pos > 0 else min(P, 4))]
        pos += len(batch)
        ab = vote_offset()
        for r in _run_pool(samplers, [probe(i, ab) for i in batch]):
            if r[0] == "x":
                diag.append(r[1:])                       # why this anchor did not match (never silent)
            else:
                matches.append((r[0], r[1]))
                sig_at_a[r[0]] = r[2]
                diag.append(("match", r[3], r[4], r[0], r[5], r[6]))

    def diag_text() -> str:
        """One compact line per anchor, in film order: where it is, what happened, how similar the best frame was,
        the offset it implies and how much contrast the playback frame has. Enough to tell a drifting timeline from
        low-contrast scenes without another run."""
        kinds: dict = {}
        for d in diag:
            kinds[d[0]] = kinds.get(d[0], 0) + 1
        rows = []
        for d in sorted(diag, key=lambda d: d[3] if len(d) > 3 and d[3] is not None else 0):
            kind, corr, _sec = d[0], d[1], d[2]
            at = d[3] if len(d) > 3 else None
            off = d[4] if len(d) > 4 else None
            con = d[5] if len(d) > 5 else 0.0
            when = "?" if at is None else "%dm%02ds" % (int(at // 60000), int(at // 1000) % 60)
            rows.append("%s@%s r=%s d=%s c=%.0f" % (
                kind[0], when, "-" if corr is None else "%.2f" % corr,
                "-" if off is None else "%+.1fs" % (off / 1000), con))
        return f"outcomes={kinds} need r>={MIN_CORR}+margin {MIN_MARGIN} | " + " ; ".join(rows)

    def counts() -> tuple[int, int]:
        return sum(sm.calls_a for sm in samplers), sum(sm.calls_b for sm in samplers)

    ab = vote_offset()
    if ab is None:
        na, nb = counts()
        return Verdict(False, "no_visual_match", anchors=0, samples_a=na, samples_b=nb, detail=diag_text())
    inl = [(pa, pb) for pa, pb in matches if abs(pb - (ab[0] * pa + ab[1])) <= MAX_RESIDUAL_MS]
    na, nb = counts()
    if len(inl) < MIN_INLIERS:
        return Verdict(False, "no_visual_match" if len(matches) < MIN_INLIERS else "nonlinear_timeline",
                       anchors=len(inl), samples_a=na, samples_b=nb, detail=diag_text())
    quarters = [sum(1 for pa, _ in inl if q * dur_a_ms / 4 <= pa < (q + 1) * dur_a_ms / 4) for q in range(4)]
    if min(quarters) < MIN_PER_QUARTER:
        return Verdict(False, "nonlinear_timeline", anchors=len(inl), samples_a=na, samples_b=nb)
    a, b, res = _constrained_fit(inl, ratio)
    # refit once without the worst point if the fit is loose, then demand the bound
    if res > MAX_RESIDUAL_MS and len(inl) > MIN_INLIERS:
        worst = max(range(len(inl)), key=lambda i: abs(inl[i][1] - (a * inl[i][0] + b)))
        inl.pop(worst)
        a, b, res = _constrained_fit(inl, ratio)
    if len(inl) < MIN_INLIERS or res > MAX_RESIDUAL_MS:
        return Verdict(False, "nonlinear_timeline", anchors=len(inl), samples_a=na, samples_b=nb)
    if abs(a - 1.0) > MAX_RATIO_ERR:
        return Verdict(False, "ratio_out_of_range", anchors=len(inl), samples_a=na, samples_b=nb)
    if samplers[0].grab_b_exact is not None:
        # When the coarse pairing cannot tell the speed hypotheses apart (short films, ~1.5 s keyframe noise), refine
        # under each plausible one and keep the better-verified; the wrong slope loses anchors / gains error.
        trials = [(a, b)] if _decisive(inl, ratio) else [(h[0], h[1]) for h in _hypotheses(inl, ratio)]
        best = None
        for a_h, b_h in trials:
            refined = _refine(inl, a_h, b_h, sig_at_a, samplers)
            if len(refined) < MIN_REFINED:
                continue
            b2 = float(np.mean([pb - a_h * pa for pa, pb in refined]))
            sigma = float(np.std([pb - (a_h * pa + b2) for pa, pb in refined]))
            fe = _refined_error(refined, a_h, b2, dur_a_ms)
            if best is None or fe < best[0]:
                best = (fe, a_h, b2, sigma, len(refined), abs(b2 - b_h))
        na, nb = counts()
        if best is not None:
            fit_err, a, b2, sigma, nref, shift = best
            if fit_err > MAX_FIT_ERR_MS or shift > MAX_RESIDUAL_MS:
                return Verdict(False, "mapping_uncertain", anchors=len(inl), samples_a=na, samples_b=nb,
                               detail=f"refined fit_err={fit_err:.0f}ms coarse_shift={shift:.0f}ms")
            return Verdict(True, "ok", Mapping(a, b2, sigma, len(inl), _classify(a, b2), fit_err), len(inl), na, nb,
                           detail=f"refined n={nref} sigma={sigma:.0f}ms a={a:.4f}")
    fit_err = _fit_error(inl, a, b, dur_a_ms, _median_gap(kfs_a), _median_gap(kfs_b))
    if fit_err > MAX_FIT_ERR_MS:
        return Verdict(False, "mapping_uncertain", anchors=len(inl), samples_a=na, samples_b=nb,
                       detail=f"fit_err={fit_err:.0f}ms residual={res:.0f}ms")
    return Verdict(True, "ok", Mapping(a, b, res, len(inl), _classify(a, b), fit_err), len(inl), na, nb)


def slot_ok(mapping: Mapping, slot_ms: int, pts_b_ms: int, limit_ms: int = 5000) -> bool:
    """Final per-thumbnail check: the decoded B frame, mapped back to A's timeline, is within the contract."""
    return abs(mapping.to_a(pts_b_ms) - slot_ms) <= limit_ms
