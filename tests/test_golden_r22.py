"""P7: radix-2^2 contract tests (re-pinned golden, spikes/S5_r22/).

The R2^2 DIF batch reference (golden.fft_fixed_batch_r22) is the new
quantization contract for the R2^2 RTL. It differs from the validated
radix-2 golden (fft_fixed_batch) by at most 1 LSB (rounding placement)
with identical SQNR; the rotation identity T[i+N/4] = -/+j*T[i] holds
bit-exactly in the canonical table (the load-bearing property that makes
the +/-j diff combine free and exact).
"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from config import FFTConfig
from golden import (fft_fixed_batch, fft_fixed_batch_dit,
                    fft_fixed_batch_r22, fft_fixed_batch_r22_dit,
                    fft_float_radix2)
from twiddles import canonical_twiddles


def _rand_frame(N, width, rng):
    hi = 2 ** (width - 1)
    return [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
            for _ in range(N)]


def _sqnr(vals, fref, N, scale):
    err = sum(abs(v[0] / scale - fref[k].real) ** 2
              + abs(v[1] / scale - fref[k].imag) ** 2
              for k, v in enumerate(vals))
    sig = sum(abs(fref[k]) ** 2 for k in range(N))
    return 10 * math.log10(sig / err) if err else float("inf")


class TestR22RotationIdentity(unittest.TestCase):
    def test_rotation_identity_bit_exact(self):
        """T[i+N/4] == -/+j * T[i] for every i, both directions, all
        widths -- the exactness of the R2^2 diff combine."""
        for N in (8, 16, 32, 64, 128, 256, 1024):
            for width, dec in ((18, 17), (12, 11), (8, 7), (18, 16)):
                for inv in (False, True):
                    tw = canonical_twiddles(N, width, dec, inverse=inv)
                    js = -1 if not inv else 1
                    for i in range(N):
                        self.assertEqual(
                            tw[(i + N // 4) % N],
                            (-js * tw[i][1], js * tw[i][0]),
                            f"rotation broken N={N} w={width} i={i}")


class TestR22BatchContract(unittest.TestCase):
    def _check(self, cfg, seed=5):
        rng = random.Random(seed)
        samples = _rand_frame(cfg.num_points, cfg.sample_width, rng)
        plain = fft_fixed_batch(samples, cfg)
        r22 = fft_fixed_batch_r22(samples, cfg)
        md = max(max(abs(p[k] - r[k]) for k in range(2))
                 for p, r in zip(plain, r22))
        # contract difference = rounding placement only: small and bounded
        # (~2 LSB at 16-bit/18-bit twiddles, growing with wider samples or
        # narrower twiddles -- both shrink the fractional headroom); a real
        # algorithmic bug would show deltas near the full output range.
        bound = max(2, 1 << max(0, cfg.sample_width - 15),
                    1 << max(0, 18 - cfg.twiddle_width))
        self.assertLessEqual(md, bound,
                             f"{cfg}: |delta|={md} > {bound}")
        # both are valid transforms: SQNR vs float within 0.5 dB
        fref = fft_float_radix2([complex(r, i) for r, i in samples])
        scale = 1 << cfg.sample_decimal
        sp, sr = _sqnr(plain, fref, cfg.num_points, scale), \
            _sqnr(r22, fref, cfg.num_points, scale)
        self.assertLessEqual(abs(sp - sr), 0.5,
                             f"{cfg}: SQNR plain={sp:.2f} r22={sr:.2f}")
        return md, sp, sr

    def test_sizes_fwd_inv(self):
        for N in (4, 8, 16, 32, 64, 128, 256):
            for inv in (False, True):
                with self.subTest(N=N, inv=inv):
                    self._check(FFTConfig(num_points=N, inverse=inv))

    def test_odd_stage_count(self):
        # odd n leaves the last stage plain -- exercised by N=8, 32, 128
        for N in (8, 32, 128):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N))

    def test_width_variants(self):
        self._check(FFTConfig(num_points=64, sample_width=8,
                              twiddle_width=8))
        self._check(FFTConfig(num_points=64, sample_width=25,
                              output_width=20))
        self._check(FFTConfig(num_points=32, twiddle_width=10,
                              twiddle_decimal=8))
        self._check(FFTConfig(num_points=16, sample_width=12,
                              output_width=20, sample_decimal=3,
                              output_decimal=2))

    def test_scaling_schedules(self):
        self._check(FFTConfig(num_points=16, scaling=(0, 0, 0, 0),
                              output_width=24))
        self._check(FFTConfig(num_points=16, scaling=(2, 0, 1, 2),
                              output_width=24))
        self._check(FFTConfig(num_points=64, scaling=(1, 2, 0, 1, 0, 1),
                              output_width=24))

    def test_n2_edge(self):
        # N=2 has a single stage: R2^2 degenerates to plain radix-2
        cfg = FFTConfig(num_points=2)
        rng = random.Random(1)
        samples = _rand_frame(2, cfg.sample_width, rng)
        self.assertEqual(fft_fixed_batch(samples, cfg),
                         fft_fixed_batch_r22(samples, cfg))


class TestR22BatchContractDit(unittest.TestCase):
    """DIT radix-2^2 contract (mirror topology, bitrev-in / native-out)."""

    @staticmethod
    def _bitrev_order(N, n):
        return [int(format(k, "0%db" % n)[::-1], 2) for k in range(N)]

    def _check(self, cfg, seed=9):
        from golden import fft_fixed_batch_dit
        rng = random.Random(seed)
        br = self._bitrev_order(cfg.num_points, cfg.num_stages)
        raw = _rand_frame(cfg.num_points, cfg.sample_width, rng)
        samples = [raw[br[j]] for j in range(cfg.num_points)]
        plain = fft_fixed_batch_dit(samples, cfg)
        r22 = fft_fixed_batch_r22_dit(samples, cfg)
        md = max(max(abs(p[k] - r[k]) for k in range(2))
                 for p, r in zip(plain, r22))
        bound = max(2, 1 << max(0, cfg.sample_width - 15),
                    1 << max(0, 18 - cfg.twiddle_width))
        self.assertLessEqual(md, bound, f"{cfg}: |delta|={md} > {bound}")
        fref = fft_float_radix2([complex(r, i) for r, i in raw])
        scale = 1 << cfg.sample_decimal
        sp = _sqnr(plain, fref, cfg.num_points, scale)
        sr = _sqnr(r22, fref, cfg.num_points, scale)
        self.assertLessEqual(abs(sp - sr), 0.5,
                             f"{cfg}: SQNR plain={sp:.2f} r22={sr:.2f}")

    def test_sizes_fwd_inv(self):
        for N in (4, 8, 16, 32, 64, 128, 256):
            for inv in (False, True):
                with self.subTest(N=N, inv=inv):
                    self._check(FFTConfig(num_points=N, inverse=inv,
                                          input_order="bitreversed",
                                          output_order="native"))

    def test_odd_stage_count(self):
        for N in (8, 32, 128):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N,
                                      input_order="bitreversed",
                                      output_order="native"))

    def test_dif_dit_consistency(self):
        """DIF-R2^2 (natural-in, bitrev-out) and DIT-R2^2 (bitrev-in,
        natural-out) compute the same transform: values agree within the
        contract noise after the output permutation."""
        rng = random.Random(11)
        for N in (8, 32, 128):
            for inv in (False, True):
                raw = _rand_frame(N, 16, rng)
                cfg_dif = FFTConfig(num_points=N, inverse=inv,
                                    input_order="native",
                                    output_order="bitreversed")
                cfg_dit = FFTConfig(num_points=N, inverse=inv,
                                    input_order="bitreversed",
                                    output_order="native")
                out_dif = fft_fixed_batch_r22(raw, cfg_dif)
                br = self._bitrev_order(N, cfg_dit.num_stages)
                out_dit = fft_fixed_batch_r22_dit([raw[br[j]]
                                                   for j in range(N)],
                                                  cfg_dit)
                md = max(max(abs(out_dif[br[k]][c] - out_dit[k][c])
                             for c in range(2)) for k in range(N))
                # two independent contracts (DIF and DIT R2^2) on the same
                # transform: agree within the rounding-placement noise
                self.assertLessEqual(md, 4, f"N={N} inv={inv}: |delta|={md}")

    def test_n2_edge(self):
        cfg = FFTConfig(num_points=2, input_order="bitreversed",
                        output_order="native")
        rng = random.Random(1)
        br = self._bitrev_order(2, cfg.num_stages)
        raw = _rand_frame(2, cfg.sample_width, rng)
        samples = [raw[br[j]] for j in range(2)]
        self.assertEqual(fft_fixed_batch_dit(samples, cfg),
                         fft_fixed_batch_r22_dit(samples, cfg))


class TestR22StreamingModel(unittest.TestCase):
    """Cycle-accurate streaming R2² DIF model must reproduce the batch
    contract bit-exactly (this is what the R2² RTL will be verified
    against)."""

    def _check(self, cfg, frames=2, seed=7):
        from golden import R22SDFGoldenModel
        rng = random.Random(seed)
        N = cfg.num_points
        samples = _rand_frame(N * frames, cfg.sample_width, rng)
        got = R22SDFGoldenModel(cfg).process_stream(samples)
        exp = []
        for f in range(frames):
            exp += fft_fixed_batch_r22(samples[f * N:(f + 1) * N], cfg)
        self.assertEqual(len(got), len(exp))
        mism = [(k, got[k], exp[k]) for k in range(len(got))
                if got[k] != exp[k]]
        self.assertEqual(mism, [], f"{cfg}: {len(mism)} stream/batch mismatches")

    def test_sizes_fwd_inv(self):
        for N in (8, 16, 32, 64, 128, 256):
            for inv in (False, True):
                with self.subTest(N=N, inv=inv):
                    self._check(FFTConfig(num_points=N, inverse=inv))

    def test_odd_stage_count(self):
        for N in (8, 32, 128):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N))

    def test_widths_and_scaling(self):
        self._check(FFTConfig(num_points=64, sample_width=8,
                              twiddle_width=8))
        self._check(FFTConfig(num_points=64, sample_width=25,
                              output_width=20))
        self._check(FFTConfig(num_points=16, scaling=(0, 0, 0, 0),
                              output_width=24))
        self._check(FFTConfig(num_points=16, twiddle_width=10,
                              twiddle_decimal=8))

    def test_multi_frame(self):
        self._check(FFTConfig(num_points=16), frames=4)
        self._check(FFTConfig(num_points=128), frames=3)

    def test_latency(self):
        from golden import R22SDFGoldenModel
        m = R22SDFGoldenModel(FFTConfig(num_points=64))
        # 6 stages -> pairs (0,1),(2,3),(4,5): 3D_0+3D_1+3D_2
        # D_0=16, D_1=4, D_2=1 -> 48+12+3 = 63
        self.assertEqual(m.latency, 63)


if __name__ == "__main__":
    unittest.main()
