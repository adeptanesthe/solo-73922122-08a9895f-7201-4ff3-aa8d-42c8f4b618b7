"""Tests for the B−A audition split (fixed-FIR vs. residual tracks).

Run:  python -m unittest tests.test_audition -v
"""

from __future__ import annotations

import io
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.audition import (  # noqa: E402
    FIR_TAPS,
    AuditionError,
    RankDeficientError,
    common_pcm16_gain,
    fit_fir,
    merge_intervals,
    render_tracks,
    split_audition,
)
from app.wavutil import PCM16_MAX, PCM16_MIN, parse_pcm16_wav, write_pcm16_wav  # noqa: E402

SR = 16000


def _pair(n: int, seed: int = 0, h=None, noise: float = 300.0, amp: float = 4000):
    rng = np.random.default_rng(seed)
    a = np.ascontiguousarray(np.rint(rng.standard_normal(n) * amp).astype(np.int16))
    if h is None:
        h = np.array([0.3, -0.15, 0.08, 0.04, -0.02] + [0.0] * (FIR_TAPS - 5))
    fixed = np.convolve(a.astype(np.float64), h, mode="full")[:n]
    residual = rng.standard_normal(n) * noise
    b = np.clip(np.rint(a.astype(np.float64) + fixed + residual),
                PCM16_MIN, PCM16_MAX).astype(np.int16)
    return a, b, h


class FitterMathTests(unittest.TestCase):
    def setUp(self):
        self.n = 4 * SR
        self.a, self.b, self.h = _pair(self.n)
        self.refs = [(int(0.2 * SR), int(1.8 * SR)),
                     (int(2.0 * SR), int(2.9 * SR))]
        self.mon = [(int(3.1 * SR), int(3.8 * SR))]

    def test_recovers_known_fir(self):
        fit = fit_fir(self.a, self.b, self.refs, self.mon)
        self.assertEqual(fit["rank"], FIR_TAPS)
        self.assertEqual(fit["taps"], FIR_TAPS)
        np.testing.assert_allclose(fit["coeffs"][:5], self.h[:5], atol=2e-3)
        self.assertGreater(fit["r2"], 0.9)

    def test_row_count_excludes_warmup_and_uses_32_inputs(self):
        fit = fit_fir(self.a, self.b, self.refs, self.mon)
        # Each reference range loses exactly taps-1 leading targets whose
        # 32-point input window starts before the range.
        expected = sum((e - s) - (FIR_TAPS - 1) for s, e in self.refs)
        self.assertEqual(fit["rows"], expected)

    def test_matches_brute_force_lstsq_with_overlap_and_monitor(self):
        n = 3 * SR
        a, b, _ = _pair(n, seed=3, amp=4000, noise=1500)
        refsegs = [(1000, 9000), (8000, 15000), (20000, 26000)]  # 2 overlaps 1
        mon = [(14000, 17000)]
        refs = merge_intervals([{"start_sample": s, "end_sample": e}
                                for s, e in refsegs], n)
        fit = fit_fir(a, b, refs, mon)

        ref_mask = np.zeros(n, dtype=bool)
        mon_mask = np.zeros(n, dtype=bool)
        for s, e in refs:
            ref_mask[s:e] = True
        for s, e in mon:
            mon_mask[s:e] = True
        af = a.astype(np.float64)
        d = b.astype(np.float64) - af
        X, y = [], []
        for k in range(n):
            pts = [k - j for j in range(FIR_TAPS)]
            if (all(p >= 0 for p in pts)
                    and all(ref_mask[p] and not mon_mask[p] for p in pts)):
                X.append(af[pts])
                y.append(d[k])
        w_ref, *_ = np.linalg.lstsq(np.array(X), np.array(y), rcond=None)
        self.assertEqual(fit["rows"], len(y))
        np.testing.assert_allclose(fit["coeffs"], w_ref, atol=1e-8)

    def test_monitor_input_contamination_drops_whole_row(self):
        # Monitor starts exactly at a reference boundary: targets whose input
        # window reaches into the monitor must be removed even though their
        # target index is still inside the reference.
        n = 2 * SR
        a, b, _ = _pair(n, seed=5)
        refs = [(1000, 20000)]
        mon = [(20000, 20500)]
        fit = fit_fir(a, b, refs, mon)
        # Reference has 19000 candidate targets; the first taps-1 cannot form
        # a full in-range input window.  Targets k=19969..19999 additionally
        # have an input point inside the monitor and are dropped, while
        # k>=20000 are outside the reference and never counted.
        expected = (20000 - 1000) - (FIR_TAPS - 1)
        self.assertEqual(fit["rows"], expected)

    def test_overlapping_references_counted_once(self):
        n = 2 * SR
        a, b, _ = _pair(n, seed=9)
        merged = merge_intervals(
            [{"start_sample": 1000, "end_sample": 9000},
             {"start_sample": 8000, "end_sample": 15000}], n)
        fit_merged = fit_fir(a, b, merged, [(16000, 17000)])
        fit_single = fit_fir(a, b, [(1000, 15000)], [(16000, 17000)])
        self.assertEqual(fit_merged["rows"], fit_single["rows"])
        np.testing.assert_allclose(fit_merged["coeffs"], fit_single["coeffs"])

    def test_rank_deficient_constant_signal(self):
        n = self.n
        a = np.full(n, 5000, dtype=np.int16)
        b = np.full(n, 5100, dtype=np.int16)
        with self.assertRaises(RankDeficientError) as ctx:
            fit_fir(a, b, self.refs, self.mon)
        self.assertLess(ctx.exception.rank, FIR_TAPS)

    def test_too_few_rows_is_rank_error(self):
        with self.assertRaises(RankDeficientError):
            fit_fir(self.a, self.b, [(0, 16)], self.mon)


class TrackRenderTests(unittest.TestCase):
    def test_decomposition_reconstructs_difference(self):
        n = 3 * SR
        a, b, h = _pair(n, seed=2)
        coeffs = fit_fir(a, b, [(1000, 20000)], [(30000, 40000)])["coeffs"]
        fixed, residual, difference = render_tracks(a, b, coeffs)
        np.testing.assert_allclose(fixed + residual, difference, rtol=0, atol=1e-6)
        # Head zero-padding: fixed[0] uses only A[0]; no wrap-around.
        self.assertAlmostEqual(fixed[0], coeffs[0] * float(a[0]), places=6)
        self.assertAlmostEqual(
            fixed[1], coeffs[0] * a[1] + coeffs[1] * a[0], places=6)

    def test_common_gain_prevents_any_clip_both_signs(self):
        rng = np.random.default_rng(11)
        t1 = rng.standard_normal(5000) * 40000
        t2 = -np.abs(rng.standard_normal(5000) * 40000)
        g = common_pcm16_gain((t1, t2))
        self.assertLess(g, 1.0)
        for tr in (t1, t2):
            q = np.rint(tr * g)
            self.assertTrue(np.all(q <= PCM16_MAX) and np.all(q >= PCM16_MIN))

    def test_unity_gain_when_headroom_exists(self):
        g = common_pcm16_gain((np.array([1000.0, -2000.0]),
                               np.array([3000.0])))
        self.assertEqual(g, 1.0)


class SplitOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.n = 4 * SR
        self.a, self.b, _ = _pair(self.n)
        self.refs = [{"start_sample": int(0.2 * SR), "end_sample": int(1.8 * SR)},
                     {"start_sample": int(2.0 * SR), "end_sample": int(2.9 * SR)}]
        self.mon = {"start_sample": int(3.1 * SR), "end_sample": int(3.8 * SR)}

    def test_full_split_and_quantized_tracks(self):
        rep = split_audition(self.a, self.b, self.refs, self.mon)
        self.assertTrue(rep["ok"])
        fixed, residual = rep["_tracks"]
        self.assertEqual(fixed.dtype, np.int16)
        self.assertEqual(residual.dtype, np.int16)
        self.assertEqual(len(fixed), self.n)
        self.assertEqual(len(residual), self.n)
        # Quantized tracks sum back to B−A within ±1 LSB (rounding).
        d = self.b.astype(np.float64) - self.a.astype(np.float64)
        err = np.abs(fixed.astype(np.float64) + residual.astype(np.float64) - d)
        self.assertLessEqual(err.max(), 1.0)
        self.assertEqual(rep["stats"]["clipped_samples"], 0)

    def test_attenuation_applied_equally_and_shown(self):
        b_big = np.clip(self.a.astype(np.float64) * 3,
                        PCM16_MIN, PCM16_MAX).astype(np.int16)
        rep = split_audition(self.a, b_big, self.refs, self.mon)
        self.assertTrue(rep["attenuated"])
        self.assertLess(rep["gain"], 1.0)
        self.assertEqual(rep["stats"]["clipped_samples"], 0)
        fixed, residual = rep["_tracks"]
        self.assertLessEqual(fixed.max(), PCM16_MAX)
        self.assertGreaterEqual(fixed.min(), PCM16_MIN)

    def test_reference_count_validation(self):
        with self.assertRaises(AuditionError):
            split_audition(self.a, self.b, [], self.mon)
        four = [{"start_sample": i * 5000, "end_sample": i * 5000 + 4000}
                for i in range(4)]
        with self.assertRaises(AuditionError):
            split_audition(self.a, self.b, four, self.mon)

    def test_overlap_rejected(self):
        bad_mon = {"start_sample": int(1.0 * SR), "end_sample": int(1.5 * SR)}
        with self.assertRaises(AuditionError):
            split_audition(self.a, self.b, self.refs, bad_mon)


class AuditionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(data_dir=self.tmp.name)
        self.client = self.app.test_client()
        n = 4 * SR
        self.a, self.b, _ = _pair(n)
        up = self.client.post(
            "/api/workspaces",
            data={
                "wav_a": (io.BytesIO(write_pcm16_wav(self.a)), "a.wav"),
                "wav_b": (io.BytesIO(write_pcm16_wav(self.b)), "b.wav"),
            },
            content_type="multipart/form-data",
            headers={"Accept": "application/json"},
        )
        self.wid = up.get_json()["workspace_id"]
        self.refs = [{"start_sample": int(0.2 * SR), "end_sample": int(1.8 * SR)},
                     {"start_sample": int(2.0 * SR), "end_sample": int(2.9 * SR)}]
        self.mon = {"start_sample": int(3.1 * SR), "end_sample": int(3.8 * SR)}

    def tearDown(self):
        self.tmp.cleanup()

    def _split(self, refs=None, mon=None):
        return self.client.post(
            f"/api/workspaces/{self.wid}/audition",
            json={"reference": refs or self.refs, "monitor": mon or self.mon},
        )

    def test_split_creates_two_playable_wavs(self):
        r = self._split()
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        rep = r.get_json()["audition"]
        self.assertIn("token", rep)
        self.assertEqual(rep["rank"], FIR_TAPS)
        for kind in ("fixed", "residual"):
            ar = self.client.get(rep["audio_urls"][kind])
            self.assertEqual(ar.status_code, 200)
            self.assertEqual(ar.mimetype, "audio/wav")
            self.assertEqual(len(parse_pcm16_wav(ar.data)), len(self.a))

    def test_resplit_invalidates_old_token(self):
        tok1 = self._split().get_json()["audition"]["token"]
        tok2 = self._split(refs=self.refs[:1]).get_json()["audition"]["token"]
        self.assertNotEqual(tok1, tok2)
        self.assertEqual(
            self.client.get(f"/workspaces/{self.wid}/audition/{tok1}/fixed").status_code,
            404)
        self.assertEqual(
            self.client.get(f"/workspaces/{self.wid}/audition/{tok2}/fixed").status_code,
            200)

    def test_rank_deficient_returns_409(self):
        n = len(self.a)
        a2 = np.full(n, 4000, np.int16)
        b2 = np.full(n, 4200, np.int16)
        up = self.client.post(
            "/api/workspaces",
            data={
                "wav_a": (io.BytesIO(write_pcm16_wav(a2)), "a.wav"),
                "wav_b": (io.BytesIO(write_pcm16_wav(b2)), "b.wav"),
            },
            content_type="multipart/form-data",
            headers={"Accept": "application/json"},
        )
        wid2 = up.get_json()["workspace_id"]
        r = self.client.post(
            f"/api/workspaces/{wid2}/audition",
            json={"reference": self.refs, "monitor": self.mon})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "rank_deficient")
        self.assertIn("秩不足", r.get_json()["error"])

    def test_bad_requests(self):
        # missing monitor
        r = self.client.post(f"/api/workspaces/{self.wid}/audition",
                             json={"reference": self.refs})
        self.assertEqual(r.status_code, 400)
        # overlap
        r = self.client.post(
            f"/api/workspaces/{self.wid}/audition",
            json={"reference": self.refs,
                  "monitor": {"start_sample": int(1.0 * SR),
                              "end_sample": int(1.5 * SR)}})
        self.assertEqual(r.status_code, 400)
        # unknown workspace
        r = self.client.post("/api/workspaces/w_nope/audition",
                             json={"reference": self.refs, "monitor": self.mon})
        self.assertEqual(r.status_code, 404)

    def test_gain_reported_when_attenuated(self):
        b_big = np.clip(self.a.astype(np.float64) * 3,
                        PCM16_MIN, PCM16_MAX).astype(np.int16)
        up = self.client.post(
            "/api/workspaces",
            data={
                "wav_a": (io.BytesIO(write_pcm16_wav(self.a)), "a.wav"),
                "wav_b": (io.BytesIO(write_pcm16_wav(b_big)), "b.wav"),
            },
            content_type="multipart/form-data",
            headers={"Accept": "application/json"},
        )
        wid = up.get_json()["workspace_id"]
        rep = self.client.post(
            f"/api/workspaces/{wid}/audition",
            json={"reference": self.refs, "monitor": self.mon}
        ).get_json()["audition"]
        self.assertTrue(rep["attenuated"])
        self.assertTrue(math.isfinite(rep["gain_db"]))
        self.assertLess(rep["gain"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
