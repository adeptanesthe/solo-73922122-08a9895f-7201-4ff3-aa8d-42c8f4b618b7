"""B−A audition split: fixed-filter component vs. residual component.

The recording engineer marks 1～3 **reference** ranges and exactly one
**monitor** range on the segment page.  The difference signal

    d_k = B_k − A_k

is modelled as the output of a *fixed* linear FIR filter acting on A:

    d_k ≈ Σ_{j=0}^{P-1} h_j · A_{k-j}        (P = 32 taps)

The same single least-squares filter ``h`` is fitted from every valid row
inside the reference ranges only.  A row with target index ``k`` is kept
only when the target *and* all 32 input samples ``A[k], A[k-1], …,
A[k-31]`` lie inside the (union of the) reference ranges and none of those
33 points falls into the monitor range.  Overlapping reference ranges are
merged first so shared samples contribute exactly once.

The normal equations are accumulated by streaming over fixed-size
:data:`WINDOW`-target windows.  Apart from the decoded audio arrays
themselves, every temporary buffer has a fixed size determined solely by
:data:`WINDOW` and the 32 taps — it does not grow with the total reference
length; only the 32×32 Gram matrix and the 32-vector are accumulated.

The two full-length audition tracks are then

    fixed_k     = Σ_j h_j · A_{k-j}        (zero-padded at the start:
                                            real A samples are reused,
                                            only head samples are zeros)
    residual_k  = d_k − fixed_k

so ``fixed + residual == B − A`` sample by sample.  If either PCM16 track
would clip after quantization, the same gain (< 1) attenuates both tracks
and is reported back for display.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .calibration import parse_segments
from .wavutil import PCM16_MAX, PCM16_MIN, SAMPLE_RATE

FIR_TAPS = 32
# Fixed scan window.  Every fitting-time buffer is sized from this constant
# (and FIR_TAPS) once, up front, so fitting memory cannot grow with the
# total reference length.
WINDOW = 256

MIN_REFERENCE_SEGMENTS = 1
MAX_REFERENCE_SEGMENTS = 3


class AuditionError(ValueError):
    """Invalid reference / monitor selection."""


class RankDeficientError(Exception):
    """The fitted design matrix does not have full column rank."""

    def __init__(self, rank: int, rows: int, taps: int):
        super().__init__(
            f"拟合矩阵秩不足：秩为 {rank}（需要 {taps}），有效方程 {rows} 组。"
            "请更换或增加参考段（选择两版均有声音、内容更丰富的区间）。"
        )
        self.rank = rank
        self.rows = rows
        self.taps = taps


# ---------------------------------------------------------------------------
# Interval bookkeeping
# ---------------------------------------------------------------------------


def merge_intervals(
    intervals: Sequence[Dict[str, Any]], n_samples: int
) -> List[Tuple[int, int]]:
    """Normalize (sample- or second-based) segment dicts and merge overlaps.

    Uses the same tolerant parser as calibration so ``start_sample`` and
    ``start``/``end`` (seconds) aliases are all accepted.  Overlapping or
    touching half-open ranges are merged into sorted, disjoint ranges.
    """
    parsed = parse_segments(intervals, n_samples)
    norm = [(int(p["start_sample"]), int(p["end_sample"])) for p in parsed]
    norm.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in norm:
        if merged and s <= merged[-1][1]:  # touching/overlapping -> one range
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _assert_disjoint(
    refs: Sequence[Tuple[int, int]],
    monitor: Sequence[Tuple[int, int]],
) -> None:
    for rs, re_ in refs:
        for ms, me in monitor:
            if rs < me and ms < re_:
                raise AuditionError(
                    f"诊听区（{ms}–{me}）与参考区（{rs}–{re_}）存在重叠，"
                    "请把诊听区圈在参考区之外。"
                )


def _mark_in_refs(
    mask: np.ndarray,
    base: int,
    intervals: Sequence[Tuple[int, int]],
    value: bool,
) -> None:
    """Mark span positions whose absolute index is inside *intervals*.

    ``mask[p]`` corresponds to absolute sample index ``base + p``.  The
    span mask has fixed length (``WINDOW + taps - 1`` at most) and is
    reused across windows; nothing of program length is allocated.
    """
    span = mask.shape[0]
    for s, e in intervals:
        lo = s - base
        hi = e - base
        if hi <= 0 or lo >= span:
            continue
        lo = max(lo, 0)
        hi = min(hi, span)
        mask[lo:hi] = value


# ---------------------------------------------------------------------------
# Streaming least-squares FIR fit
# ---------------------------------------------------------------------------


def fit_fir(
    a: np.ndarray,
    b: np.ndarray,
    reference: Sequence[Tuple[int, int]],
    monitor: Sequence[Tuple[int, int]],
    taps: int = FIR_TAPS,
) -> Dict[str, Any]:
    """Fit the common FIR filter with fixed, length-independent memory.

    The normal equations ``XᵀX h = Xᵀy`` are accumulated by streaming over
    the reference ranges in :data:`WINDOW`-sized windows.  Every buffer used
    by the hot loop is allocated once here and has a size determined solely
    by :data:`WINDOW` and *taps*; apart from the decoded audio arrays
    themselves, no temporary grows with the reference length.

    A target row ``k`` is accepted only when ``k`` and all *taps* input
    points (``k … k-taps+1``) lie inside the reference union and outside
    the monitor range; merged references guarantee every sample counts once.

    Returns ``{"coeffs", "rows", "rank", "sigma", "r2", "taps"}`` and raises
    :class:`RankDeficientError` for a rank-deficient design matrix.
    """
    af = a.astype(np.float64, copy=False)
    bf = b.astype(np.float64, copy=False)

    # ---- Fixed-size working buffers (allocated once, reused forever) -----
    span_len = WINDOW + taps - 1
    in_ref = np.zeros(span_len, dtype=bool)
    in_mon = np.zeros(span_len, dtype=bool)
    row_ok = np.zeros(WINDOW, dtype=bool)
    tmp_bool = np.zeros(WINDOW, dtype=bool)
    not_mon = np.zeros(WINDOW, dtype=bool)
    arange_buf = np.arange(WINDOW, dtype=np.int64)  # never modified
    idx_buf = np.empty(WINDOW, dtype=np.int64)
    target_buf = np.empty(WINDOW, dtype=np.int64)
    clip_buf = np.empty(WINDOW, dtype=np.int64)
    xg = np.empty((WINDOW, taps), dtype=np.float64)
    yg = np.empty(WINDOW, dtype=np.float64)
    bf_buf = np.empty(WINDOW, dtype=np.float64)
    af_buf = np.empty(WINDOW, dtype=np.float64)
    gram = np.zeros((taps, taps), dtype=np.float64)
    gram_tmp = np.empty((taps, taps), dtype=np.float64)
    rhs = np.zeros(taps, dtype=np.float64)
    rhs_tmp = np.empty(taps, dtype=np.float64)

    rows = 0
    target_ss = 0.0
    for r_start, r_end in reference:
        # The first taps-1 targets of each range cannot have all inputs
        # inside the range; they are rejected by the span mask anyway.
        k = r_start
        while k < r_end:
            m = min(WINDOW, r_end - k)
            base = k - (taps - 1)  # absolute index of span position 0

            in_ref[: m + taps - 1].fill(False)
            in_mon[: m + taps - 1].fill(False)
            _mark_in_refs(in_ref, base, reference, True)
            _mark_in_refs(in_mon, base, monitor, True)

            # Row k+i needs span positions (taps-1+i) … i (i.e. inputs
            # k+i … k+i-taps+1); all must be reference, none monitor.
            row_ok[:m].fill(True)
            for j in range(taps):
                # span position of input k+i-j: taps-1+i-j
                lo = taps - 1 - j
                np.logical_and(
                    row_ok[:m], in_ref[lo: lo + m], out=tmp_bool[:m]
                )
                np.logical_not(in_mon[lo: lo + m], out=not_mon[:m])
                np.logical_and(
                    tmp_bool[:m], not_mon[:m], out=row_ok[:m]
                )

            n_keep = int(np.count_nonzero(row_ok[:m]))
            if n_keep:
                # Window-local positions of the kept rows.  arange_buf is
                # read-only and must not alias idx_buf (the output buffer).
                np.compress(row_ok[:m], arange_buf[:m], out=idx_buf[:n_keep])
                # Absolute target indices in a reused buffer (no new array).
                np.add(idx_buf[:n_keep], k, out=target_buf[:n_keep])
                targets_abs = target_buf[:n_keep]
                for j in range(taps):
                    # Gather A[k+i-j] into a fixed buffer.  A negative index
                    # would wrap, so clip the gather indices to 0; the span
                    # mask guarantees every accepted row has non-negative
                    # inputs, so a clipped value can never enter a kept row.
                    np.subtract(targets_abs, j, out=clip_buf[:n_keep])
                    np.maximum(clip_buf[:n_keep], 0, out=clip_buf[:n_keep])
                    np.take(af, clip_buf[:n_keep], out=xg[:n_keep, j])
                # Target y = B−A at the kept target indices, formed inside
                # fixed-size buffers (no length-dependent difference array).
                np.take(bf, targets_abs, out=bf_buf[:n_keep])
                np.take(af, targets_abs, out=af_buf[:n_keep])
                np.subtract(bf_buf[:n_keep], af_buf[:n_keep],
                            out=yg[:n_keep])

                Xv = xg[:n_keep]
                yv = yg[:n_keep]
                np.dot(Xv.T, Xv, out=gram_tmp)
                gram += gram_tmp
                np.dot(Xv.T, yv, out=rhs_tmp)
                rhs += rhs_tmp
                target_ss += float(np.dot(yv, yv))
                rows += n_keep
            k += m

    if rows < taps:
        raise RankDeficientError(rank=min(rows, taps - 1) if rows else 0,
                                 rows=rows, taps=taps)

    # Rank check on the (symmetric, PSD) Gram matrix via its singular values.
    sv = np.linalg.svd(gram, compute_uv=False)
    tol = sv[0] * max(gram.shape) * np.finfo(np.float64).eps
    rank = int(np.count_nonzero(sv > tol))
    if rank < taps:
        raise RankDeficientError(rank=rank, rows=rows, taps=taps)

    w = np.linalg.solve(gram, rhs)

    # Residual sum of squares via the normal-equation identity
    #   ‖y − Xw‖² = yᵀy − wᵀXᵀy  (XᵀX w = Xᵀy),
    # so no second pass over the rows (and no extra large buffer) is needed.
    resid_ss = target_ss - float(np.dot(w, rhs))
    resid_ss = max(resid_ss, 0.0)
    sigma = math.sqrt(resid_ss / rows) if rows else 0.0
    r2 = 1.0 - resid_ss / target_ss if target_ss > 0 else 0.0

    # Column layout is [A_k, A_{k-1}, …, A_{k-31}], matching the impulse
    # order expected by np.convolve.
    coeffs = np.asarray(w, dtype=np.float64)
    return {
        "coeffs": coeffs,
        "rows": rows,
        "rank": rank,
        "sigma": sigma,
        "r2": r2,
        "taps": taps,
        "singular_min": float(sv[-1]),
    }


# ---------------------------------------------------------------------------
# Track rendering + shared anti-clip gain
# ---------------------------------------------------------------------------


def render_tracks(
    a: np.ndarray,
    b: np.ndarray,
    coeffs: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return full-length float64 ``(fixed, residual, difference)``.

    The fixed filter reuses the original preceding A samples; samples before
    the program start are taken as zero (head zero-padding only).  Inputs
    are converted to float64 here — these are decoded-audio-scale arrays,
    not fitting temporaries.
    """
    af = a.astype(np.float64, copy=False)
    bf = b.astype(np.float64, copy=False)
    difference = bf - af
    fixed = np.convolve(af, coeffs, mode="full")[: a.shape[0]]
    residual = difference - fixed
    return fixed, residual, difference


def common_pcm16_gain(tracks: Sequence[np.ndarray]) -> float:
    """Largest gain ``g ≤ 1`` keeping every rounded sample in PCM16 range.

    PCM16 is asymmetric (``[-32768, 32767]``) and :func:`numpy.rint` rounds
    halves to even, so an exact +32767.5 would round up to 32768.  The
    boundaries are therefore pulled inward by one ULP of float64.  The gain
    is computed against both signs of every track so neither quantized
    track can clip.
    """
    eps = 1e-9
    pos_peak = 0.0
    neg_peak = 0.0
    for tr in tracks:
        if tr.size:
            p = float(tr.max())
            m = float((-tr).max())
            if p > pos_peak:
                pos_peak = p
            if m > neg_peak:
                neg_peak = m
    limits = []
    if pos_peak > 0.0:
        limits.append((32767.5 - eps) / pos_peak)
    if neg_peak > 0.0:
        limits.append((32768.5 - eps) / neg_peak)
    if not limits:
        return 1.0
    return min(1.0, min(limits))


def quantize_pair(
    fixed: np.ndarray,
    residual: np.ndarray,
    gain: float,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, float]]:
    """Apply the common gain and quantize both tracks to int16.

    Returns the two int16 arrays plus peak statistics (pre/post gain, on the
    unquantized signals).  A defensive clip is applied; with the gain from
    :func:`common_pcm16_gain` it never triggers.
    """
    f_scaled = fixed * gain
    r_scaled = residual * gain
    f_q = np.rint(f_scaled)
    r_q = np.rint(r_scaled)
    clipped = int(np.count_nonzero((f_q > PCM16_MAX) | (f_q < PCM16_MIN))
                  + np.count_nonzero((r_q > PCM16_MAX) | (r_q < PCM16_MIN)))
    f_i16 = np.clip(f_q, PCM16_MIN, PCM16_MAX).astype("<i2")
    r_i16 = np.clip(r_q, PCM16_MIN, PCM16_MAX).astype("<i2")
    stats = {
        "raw_peak_fixed": float(np.max(np.abs(fixed))) if fixed.size else 0.0,
        "raw_peak_residual": float(np.max(np.abs(residual))) if residual.size else 0.0,
        "scaled_peak_fixed": float(np.max(np.abs(f_scaled))) if f_scaled.size else 0.0,
        "scaled_peak_residual": float(np.max(np.abs(r_scaled))) if r_scaled.size else 0.0,
        "clipped_samples": float(clipped),
    }
    return f_i16, r_i16, stats


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def split_audition(
    a: np.ndarray,
    b: np.ndarray,
    reference_segments: Sequence[Dict[str, Any]],
    monitor_segment: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate roles, fit the FIR and render both audition tracks.

    Returns a report containing the int16 tracks, the common gain and
    fitting diagnostics.  Raises :class:`AuditionError` for bad selections
    and :class:`RankDeficientError` for a rank-deficient fit.
    """
    n = int(a.shape[0])
    if not (MIN_REFERENCE_SEGMENTS <= len(reference_segments)
            <= MAX_REFERENCE_SEGMENTS):
        raise AuditionError(
            f"参考段必须为 {MIN_REFERENCE_SEGMENTS}～{MAX_REFERENCE_SEGMENTS} 段，"
            f"当前提交了 {len(reference_segments)} 段。"
        )
    refs = merge_intervals(reference_segments, n)
    monitor = merge_intervals([monitor_segment], n)
    if len(monitor) != 1:
        raise AuditionError("诊听区必须恰好为 1 段。")
    _assert_disjoint(refs, monitor)

    # Decode-scale float arrays (one copy of the program each); everything
    # inside fit_fir beyond these inputs is fixed-size regardless of length.
    af = a.astype(np.float64, copy=False)
    bf = b.astype(np.float64, copy=False)
    fit = fit_fir(af, bf, refs, monitor)
    coeffs = fit["coeffs"]

    fixed_f, residual_f, difference_f = render_tracks(af, bf, coeffs)
    gain = common_pcm16_gain((fixed_f, residual_f))
    fixed_i16, residual_i16, stats = quantize_pair(fixed_f, residual_f, gain)

    # Sanity: fixed + residual must reconstruct B−A exactly up to floating
    # point round-off (residual is defined as difference − fixed).
    if not np.allclose(fixed_f + residual_f, difference_f, rtol=0, atol=1e-6):  # pragma: no cover
        raise RuntimeError("诊听轨分解校验失败：fixed + residual ≠ B−A")

    mon_s, mon_e = monitor[0]
    mon_d = difference_f[mon_s:mon_e]
    mon_f = fixed_f[mon_s:mon_e]
    mon_r = residual_f[mon_s:mon_e]

    def _rms(x: np.ndarray) -> float:
        return float(np.sqrt(np.mean(x * x))) if x.size else 0.0

    report = {
        "ok": True,
        "taps": int(fit["taps"]),
        "rows": int(fit["rows"]),
        "rank": int(fit["rank"]),
        "r2": float(fit["r2"]),
        "sigma": float(fit["sigma"]),
        "coeffs": [float(c) for c in coeffs],
        "gain": float(gain),
        "gain_db": float(20.0 * math.log10(gain)) if gain > 0 else float("-inf"),
        "attenuated": bool(gain < 1.0),
        "reference_ranges": [[s, e] for s, e in refs],
        "monitor_range": [mon_s, mon_e],
        "monitor": {
            "start": mon_s / SAMPLE_RATE,
            "end": mon_e / SAMPLE_RATE,
            "rms_difference": _rms(mon_d),
            "rms_fixed": _rms(mon_f * gain),
            "rms_residual": _rms(mon_r * gain),
        },
        "stats": stats,
        "sample_rate": SAMPLE_RATE,
        "samples": n,
    }
    report["_tracks"] = (fixed_i16, residual_i16)
    return report
