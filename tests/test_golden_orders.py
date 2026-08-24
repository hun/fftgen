"""P3: full ordering composition (all four corners, PLAN.md 2.3).

  native   -> bitreversed : DIF core
  bitreversed -> native   : DIT core
  native   -> native      : DIF core + output reorder
  bitreversed -> bitrev   : DIT core + output reorder
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig
from golden import (OrderedFFTModel, fft_fixed_batch, fft_fixed_batch_dit)
from stimuli import random_frame

CORNERS = [("native", "bitreversed"), ("bitreversed", "native"),
           ("native", "native"), ("bitreversed", "bitreversed")]


def bitrev_permute(seq):
    N = len(seq)
    b = N.bit_length() - 1
    out = [None] * N
    for k, v in enumerate(seq):
        out[int(format(k, f"0{b}b")[::-1], 2)] = v
    return out


def expected(frame, cfg):
    """Batch reference for a whole frame under the cfg's order corner."""
    io, oo = cfg.input_order, cfg.output_order
    base = (fft_fixed_batch(frame, cfg) if io == "native"
            else fft_fixed_batch_dit(bitrev_permute(frame), cfg))
    need_br = (oo == "bitreversed") != (io == "native")
    return bitrev_permute(base) if need_br else base


class TestOrderedModel(unittest.TestCase):
    def test_all_corners_bit_exact(self):
        rng = random.Random(21)
        for N in (2, 4, 8, 16, 32):
            for io, oo in CORNERS:
                with self.subTest(N=N, io=io, oo=oo):
                    cfg = FFTConfig(num_points=N, input_order=io,
                                    output_order=oo)
                    frames = [random_frame(N, 16, rng) for _ in range(3)]
                    exp = []
                    for fr in frames:
                        exp.extend(expected(fr, cfg))
                    samples = [s for fr in frames for s in
                               (bitrev_permute(fr) if io == "bitreversed"
                                else fr)]
                    got = OrderedFFTModel(cfg).process_stream(samples)
                    self.assertEqual(len(got), len(samples))
                    self.assertEqual(got, exp)

    def test_inverse_all_corners(self):
        rng = random.Random(33)
        for N in (8, 16):
            for io, oo in CORNERS:
                with self.subTest(N=N, io=io, oo=oo):
                    cfg = FFTConfig(num_points=N, inverse=True,
                                    input_order=io, output_order=oo)
                    frame = random_frame(N, 16, rng)
                    inp = (bitrev_permute(frame) if io == "bitreversed"
                           else frame)
                    got = OrderedFFTModel(cfg).process_stream(inp * 3)
                    exp = expected(frame, cfg)
                    self.assertEqual(tuple(got[-N:]), tuple(exp))

    def test_marker_alignment_all_corners(self):
        N = 8
        for io, oo in CORNERS:
            with self.subTest(io=io, oo=oo):
                cfg = FFTConfig(num_points=N, input_order=io,
                                output_order=oo)
                rng = random.Random(9)
                frames = [random_frame(N, 16, rng) for _ in range(2)]
                markers = [(1 if j == 0 else 0, 1 if j == N - 1 else 0)
                           for _ in range(2) for j in range(N)]
                samples = [s for fr in frames for s in
                           (bitrev_permute(fr) if io == "bitreversed" else fr)]
                out = OrderedFFTModel(cfg).process_stream(samples,
                                                          markers=markers)
                for fi in range(2):
                    chunk = out[fi * N:(fi + 1) * N]
                    self.assertEqual(chunk[0][2], 1, f"SOF frame {fi} {io}->{oo}")
                    self.assertEqual(chunk[-1][3], 1, f"EOF frame {fi} {io}->{oo}")

    def test_latency(self):
        # reorder corners add one frame (N) to the core latency
        cfg_no = FFTConfig(num_points=8)
        cfg_ro = FFTConfig(num_points=8, input_order="native",
                           output_order="native")
        self.assertEqual(OrderedFFTModel(cfg_no).latency,
                         OrderedFFTModel(cfg_ro).latency - 8)


if __name__ == "__main__":
    unittest.main()
