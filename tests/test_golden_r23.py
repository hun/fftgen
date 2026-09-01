"""S7: radix-2^3 contract tests (merged DIF triples, spikes/S7_r23/).

The R2^3 DIF batch reference (golden.fft_fixed_batch_r23) merges each
stage triple (3m..3m+2) into one 8-sample group with SEVEN products
(one per non-trivial output) through a single shared complex multiplier,
plus two fabric 45-degree rotates (r = rot45(d_odd)). It differs from
the validated radix-2 golden (fft_fixed_batch) by a few LSB (rounding
placement of the fused triple products) with identical SQNR -- the same
re-pinning situation as the R2^2 contract (P7).

The streaming model (golden.R23SDFGoldenModel, stage latency 7G+2)
reproduces the batch contract bit-exactly; it is the schedule contract
the R2^3 RTL must mirror (spikes/S7_r23/notes.md layer table).
"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from config import FFTConfig
from golden import fft_fixed_batch, fft_fixed_batch_r23, R23SDFGoldenModel


def _rand_frame(n, width, rng):
    hi = 2 ** (width - 1)
    return [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
            for _ in range(n)]


def _sqnr(vals, fref, N, scale):
    err = sum(abs(v[0] / scale - fref[k].real) ** 2
              + abs(v[1] / scale - fref[k].imag) ** 2
              for k, v in enumerate(vals))
    sig = sum(abs(fref[k]) ** 2 for k in range(N))
    return 10 * math.log10(sig / err) if err else float("inf")


class TestR23BatchContract(unittest.TestCase):
    def _check(self, cfg, seed=5):
        rng = random.Random(seed)
        samples = _rand_frame(cfg.num_points, cfg.sample_width, rng)
        plain = fft_fixed_batch(samples, cfg)
        r23 = fft_fixed_batch_r23(samples, cfg)
        md = max(max(abs(p[k] - r[k]) for k in range(2))
                 for p, r in zip(plain, r23))
        # contract difference = rounding placement only: small and bounded
        # (~2-3 LSB at 16-bit/18-bit twiddles, growing with wider samples
        # or narrower twiddles -- same law as the R2^2 re-pin); an
        # algorithmic bug would show deltas near the full output range.
        bound = max(2, 1 << max(0, cfg.sample_width - 15),
                    1 << max(0, 18 - cfg.twiddle_width))
        self.assertLessEqual(md, bound, f"{cfg}: |delta|={md} > {bound}")
        fref = __import__("golden").fft_float_reference(
            [complex(re, im) for re, im in samples], cfg.inverse)
        scale = float(1 << cfg.sample_decimal)
        sp = _sqnr(plain, fref, cfg.num_points, scale)
        sr = _sqnr(r23, fref, cfg.num_points, scale)
        self.assertAlmostEqual(sp, sr, delta=0.05,
                               msg=f"{cfg}: SQNR {sp} vs {sr}")

    def test_sizes_fwd_inv(self):
        """Covers leftover counts: n mod 3 = 0 (N=8,64,512), 1 (16,128,
        1024), 2 (32,256)."""
        for N in (8, 16, 32, 64, 128, 256, 512, 1024):
            for inv in (False, True):
                with self.subTest(N=N, inv=inv):
                    self._check(FFTConfig(num_points=N, inverse=inv))

    def test_width_variants(self):
        self._check(FFTConfig(num_points=64, sample_width=8,
                              twiddle_width=8))
        self._check(FFTConfig(num_points=64, sample_width=25,
                              output_width=20))
        self._check(FFTConfig(num_points=16, twiddle_width=10,
                              twiddle_decimal=8))

    def test_scaling_schedules(self):
        self._check(FFTConfig(num_points=16, scaling=(0, 0, 0, 0),
                              output_width=24))
        self._check(FFTConfig(num_points=64, scaling=(1, 0, 2, 1, 0, 1)))
        self._check(FFTConfig(num_points=32, scaling=(2, 2, 2, 2, 2)))


class TestR23StreamingModel(unittest.TestCase):
    """Cycle-accurate streaming R2^3 model must reproduce the batch
    contract bit-exactly (this is what the R2^3 RTL will be verified
    against)."""

    def _check(self, cfg, frames=2, seed=7, markers=False):
        rng = random.Random(seed)
        N = cfg.num_points
        samples = _rand_frame(N * frames, cfg.sample_width, rng)
        mk = None
        if markers:
            mk = [(1 if k % N == 0 else 0, 1 if k % N == N - 1 else 0)
                  for k in range(N * frames)]
        model = R23SDFGoldenModel(cfg)
        got = model.process_stream(samples, markers=mk)
        exp = []
        for f in range(frames):
            exp += fft_fixed_batch_r23(samples[f * N:(f + 1) * N], cfg)
        self.assertEqual(len(got), len(exp))
        mism = [(k, got[k], exp[k]) for k in range(len(got))
                if tuple(got[k][:2]) != tuple(exp[k][:2])]
        self.assertEqual(mism, [], f"{cfg}: {len(mism)} stream/batch "
                                   f"mismatches, first at {mism[:1]}")
        if mk is not None:
            for f in range(frames):
                self.assertEqual(got[f * N][2], 1, "SOF marker")
                self.assertEqual(got[f * N + N - 1][3], 1, "EOF marker")

    def test_sizes_fwd_inv(self):
        for N in (8, 16, 32, 64, 128, 256, 512, 1024):
            for inv in (False, True):
                with self.subTest(N=N, inv=inv):
                    self._check(FFTConfig(num_points=N, inverse=inv))

    def test_widths_and_scaling(self):
        self._check(FFTConfig(num_points=64, sample_width=8,
                              twiddle_width=8))
        self._check(FFTConfig(num_points=64, sample_width=25,
                              output_width=20))
        self._check(FFTConfig(num_points=16, scaling=(0, 0, 0, 0),
                              output_width=24))
        self._check(FFTConfig(num_points=16, twiddle_width=10,
                              twiddle_decimal=8))
        self._check(FFTConfig(num_points=256, scaling=(2, 1, 0, 1, 2, 1,
                                                       0, 1)))

    def test_multi_frame_and_markers(self):
        self._check(FFTConfig(num_points=16), frames=4, markers=True)
        self._check(FFTConfig(num_points=128), frames=3, markers=True)
        self._check(FFTConfig(num_points=32, inverse=True), frames=3,
                    markers=True)

    def test_latency(self):
        # stage latency 7G+2; N=64: triples G=8 and G=1 -> 58 + 9 = 67
        m = R23SDFGoldenModel(FFTConfig(num_points=64))
        self.assertEqual(m.latency, 67)
        # N=8: single triple G=1 -> 9
        m = R23SDFGoldenModel(FFTConfig(num_points=8))
        self.assertEqual(m.latency, 9)
        # N=16: triple G=2 (16) + leftover D=1 stage (1+10): 16+11 = 27
        m = R23SDFGoldenModel(FFTConfig(num_points=16))
        self.assertEqual(m.latency, 27)
        # N=32: triple G=4 (30) + leftovers D=2 (12) + D=1 (11): 53
        m = R23SDFGoldenModel(FFTConfig(num_points=32))
        self.assertEqual(m.latency, 53)

    def test_tick_interface(self):
        """tick() lockstep matches process_stream (freeze-ready API)."""
        cfg = FFTConfig(num_points=16)
        rng = random.Random(11)
        samples = _rand_frame(32, cfg.sample_width, rng)
        m1 = R23SDFGoldenModel(cfg).process_stream(samples)
        m2 = R23SDFGoldenModel(cfg)
        out = []
        for re, im in samples:
            v, r, i, u, l = m2.tick(True, re, im)
            if v:
                out.append((r, i))
        for _ in range(m2.latency):
            v, r, i, u, l = m2.tick(True)
            if v:
                out.append((r, i))
        self.assertEqual(out, m1)


if __name__ == "__main__":
    unittest.main()
