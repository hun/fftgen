"""P3: DIT topology (bit-reversed input -> native output) golden models.

The DIT datapath is the mirror of DIF: twiddle multiplies the newer input
BEFORE the butterfly, depths grow toward the output side (2^s), and the
output is natural order. Quantization contract (see golden.py): exact
Karatsuba product, then combine at 2^td scale, ONE fused rounding shift.
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig
from golden import (SDFGoldenModel, fft_fixed_batch_dit,
                    fft_float_radix2)
from stimuli import random_frame

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


def bitrev_permute(seq):
    N = len(seq)
    b = N.bit_length() - 1
    out = [None] * N
    for k, v in enumerate(seq):
        out[int(format(k, f"0{b}b")[::-1], 2)] = v
    return out


def snr_db(ref, dut):
    sig = sum(abs(v) ** 2 for v in ref)
    err = sum(abs(r - d) ** 2 for r, d in zip(ref, dut))
    if err == 0:
        return float("inf")
    return 10 * __import__("math").log10(sig / err)


class TestBatchDIT(unittest.TestCase):
    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_vs_numpy(self):
        rng = random.Random(3)
        for N in (4, 8, 16, 32):
            cfg = FFTConfig(num_points=N, input_order="bitreversed",
                            output_order="native")
            frame = random_frame(N, 16, rng)
            out = fft_fixed_batch_dit(bitrev_permute(frame), cfg)
            ref = np.fft.fft(np.array([complex(*v) for v in frame])) / N
            self.assertGreater(snr_db(ref, [complex(*v) for v in out]), 55.0, N)

    def test_duality_with_dif(self):
        """DIT(bitrev(x)) must equal DIF(x) slot-for-slot within rounding.

        The two topologies quantize at different points (DIF rounds the
        product; DIT rounds the combine), so the duality is exact only up
        to ~1 LSB per path -- compare with SNR, not bit-equality.
        """
        from golden import fft_fixed_batch
        rng = random.Random(7)
        for N in (4, 16, 64):
            cfg_dif = FFTConfig(num_points=N)
            cfg_dit = FFTConfig(num_points=N, input_order="bitreversed",
                                output_order="native")
            frame = random_frame(N, 16, rng)
            a = fft_fixed_batch(frame, cfg_dif)              # slots bitrev
            b = fft_fixed_batch_dit(bitrev_permute(frame), cfg_dit)  # natural
            # align: DIT slot k == DIF slot bitrev(k)
            b_br = bitrev_permute(b)
            self.assertGreater(
                snr_db([complex(*v) for v in a],
                       [complex(*v) for v in b_br]),
                60.0, N)

    def test_impulse_flat(self):
        # impulse amplitude A: flat spectrum A; scaled core reports A/N
        N = 16
        cfg = FFTConfig(num_points=N, input_order="bitreversed",
                        output_order="native")
        amp = 1 << 12
        frame = [(amp, 0)] + [(0, 0)] * (N - 1)
        out = fft_fixed_batch_dit(bitrev_permute(frame), cfg)
        expected = amp >> cfg.num_stages
        for re, im in out:
            self.assertEqual((re, im), (expected, 0))


class TestStreamDIT(unittest.TestCase):
    def _check(self, cfg, num_frames=2, seed=5):
        rng = random.Random(seed)
        N = cfg.num_points
        frames = [random_frame(N, cfg.sample_width, rng)
                  for _ in range(num_frames)]
        expected = []
        for fr in frames:
            expected.extend(fft_fixed_batch_dit(bitrev_permute(fr), cfg))
        samples = [s for fr in frames for s in bitrev_permute(fr)]
        got = SDFGoldenModel(cfg, dit=True).process_stream(samples)
        self.assertEqual(len(got), len(expected))
        for i, (g, e) in enumerate(zip(got, expected)):
            self.assertEqual(g, e, f"sample {i}")

    def test_sizes(self):
        for N in (2, 4, 8, 16, 32, 64, 128):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, input_order="bitreversed",
                                      output_order="native"))

    def test_inverse(self):
        for N in (8, 32):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, inverse=True,
                                      input_order="bitreversed",
                                      output_order="native"))

    def test_width_and_scaling(self):
        self._check(FFTConfig(num_points=16, input_order="bitreversed",
                              output_order="native", sample_width=8,
                              twiddle_width=8))
        self._check(FFTConfig(num_points=8, input_order="bitreversed",
                              output_order="native", scaling=(0, 0, 0),
                              output_width=24))

    def test_marker_alignment(self):
        N = 8
        cfg = FFTConfig(num_points=N, input_order="bitreversed",
                        output_order="native")
        rng = random.Random(9)
        frames = [random_frame(N, 16, rng) for _ in range(2)]
        markers = [(1 if j == 0 else 0, 1 if j == N - 1 else 0)
                   for _ in range(2) for j in range(N)]
        samples = [s for fr in frames for s in bitrev_permute(fr)]
        out = SDFGoldenModel(cfg, dit=True).process_stream(samples,
                                                           markers=markers)
        for fi in range(2):
            chunk = out[fi * N:(fi + 1) * N]
            self.assertEqual(chunk[0][2], 1, f"SOF frame {fi}")
            self.assertEqual(chunk[-1][3], 1, f"EOF frame {fi}")


class TestTopologySelection(unittest.TestCase):
    def test_dit_requires_bitrev_in(self):
        cfg = FFTConfig(num_points=8)   # native in
        with self.assertRaises(NotImplementedError):
            SDFGoldenModel(cfg, dit=True)

    def test_dif_requires_native_in(self):
        cfg = FFTConfig(num_points=8, input_order="bitreversed")
        with self.assertRaises(NotImplementedError):
            SDFGoldenModel(cfg)


if __name__ == "__main__":
    unittest.main()
