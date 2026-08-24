import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from twiddles import (canonical_twiddles,
                      decode_quarter_wave, quarter_wave_rom)


def ideal(k, N):
    ang = 2.0 * math.pi * k / N
    return math.cos(ang), -math.sin(ang)   # forward orientation


class TestCanonicalTable(unittest.TestCase):
    def test_close_to_ideal(self):
        for N in (4, 8, 16, 32, 64):
            width, dec = 18, 17
            scale = 1 << dec
            tab = canonical_twiddles(N, width, dec)
            for k in range(N):
                rc, ic = ideal(k, N)
                re, im = tab[k]
                self.assertLess(abs(re / scale - rc), 2.0 / scale, (N, k))
                self.assertLess(abs(im / scale - ic), 2.0 / scale, (N, k))

    def test_component_magnitude_bounded_by_one(self):
        # overflow-proofness of the datapath relies on this
        scale = 1 << 17
        for N in (4, 8, 64, 256):
            for re, im in canonical_twiddles(N, 18, 17):
                self.assertLessEqual(abs(re), scale)
                self.assertLessEqual(abs(im), scale)

    def test_unit_endpoints_saturate_sub_unity(self):
        hi = (1 << 17) - 1
        t0 = canonical_twiddles(16, 18, 17)[0]
        self.assertEqual(t0, (hi, 0))            # W^0 ~ +1 (biased down)
        tn = canonical_twiddles(16, 18, 17)[8]
        self.assertEqual(tn, (-hi, 0))           # W^(N/2) ~ -1
        tq = canonical_twiddles(16, 18, 17)[4]
        self.assertEqual(tq, (0, -hi))           # W^(N/4) = -j

    def test_inverse_conjugate(self):
        N, w, d = 32, 18, 17
        fwd = canonical_twiddles(N, w, d, inverse=False)
        inv = canonical_twiddles(N, w, d, inverse=True)
        for k in range(N):
            self.assertEqual(fwd[k][0], inv[k][0])
            self.assertEqual(fwd[k][1], -inv[k][1])
        self.assertEqual(inv[N // 4], (0, (1 << d) - 1))  # rail-safe conj


class TestQuarterWaveDecode(unittest.TestCase):
    def test_decode_equals_canonical_exhaustive(self):
        for N in (4, 8, 16, 32, 64, 128, 256):
            A, B = quarter_wave_rom(N, 18, 17)
            tab = canonical_twiddles(N, 18, 17)
            self.assertEqual(len(A), N // 4 + 1)
            for k in range(N):
                self.assertEqual(decode_quarter_wave(k, N, A, B), tab[k], (N, k))

    def test_decode_inverse(self):
        for N in (8, 32, 128):
            A, B = quarter_wave_rom(N, 18, 17)
            tab = canonical_twiddles(N, 18, 17, inverse=True)
            for k in range(N):
                self.assertEqual(decode_quarter_wave(k, N, A, B, True), tab[k])

    def test_narrow_widths_too(self):
        for w, d in ((8, 6), (10, 7), (12, 11)):
            for N in (4, 16, 64):
                A, B = quarter_wave_rom(N, w, d)
                tab = canonical_twiddles(N, w, d)
                for k in range(N):
                    self.assertEqual(decode_quarter_wave(k, N, A, B), tab[k])

    def test_requires_min_n(self):
        with self.assertRaises(ValueError):
            quarter_wave_rom(2, 18, 17)


class TestDeterminism(unittest.TestCase):
    def test_repeated_generation_identical(self):
        a = canonical_twiddles(64, 16, 14)
        b = canonical_twiddles(64, 16, 14)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
