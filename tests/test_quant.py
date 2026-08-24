import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from quant import (round_shift, saturate, round_shift_sat,
                   complex_multiply_karatsuba, quantize_output, wrap)


class TestRoundShift(unittest.TestCase):
    def test_identity_at_zero(self):
        for v in (0, 1, -1, 12345, -12345):
            self.assertEqual(round_shift(v, 0), v)

    def test_negative_shift_is_exact_left(self):
        self.assertEqual(round_shift(5, -2), 20)
        self.assertEqual(round_shift(-3, -1), -6)

    def test_round_half_up_positive(self):
        self.assertEqual(round_shift(8, 1), 4)
        self.assertEqual(round_shift(9, 1), 5)     # 4.5 -> 5 (half up)
        self.assertEqual(round_shift(7, 1), 4)     # 3.5 -> 4
        self.assertEqual(round_shift(3, 2), 1)     # 0.75 -> 1
        self.assertEqual(round_shift(2, 2), 1)     # 0.5 -> 1 (tie up)

    def test_round_half_up_negative(self):
        # arithmetic shift semantics: floor(v/2^s + 0.5)
        self.assertEqual(round_shift(-8, 1), -4)
        self.assertEqual(round_shift(-7, 1), -3)   # -3.5 -> -3 (toward +inf tie)
        self.assertEqual(round_shift(-9, 1), -4)   # -4.5 -> -4
        self.assertEqual(round_shift(-1, 1), 0)    # -0.5 -> 0
        self.assertEqual(round_shift(-3, 2), -1)   # -0.75 -> -1? floor(-0.75+0.25)=floor(-0.5)... see below
        # -3: (-3 + 1) >> 2 = -2 >> 2 = -1  (floor(-0.5) = -1) OK

    def test_matches_rtl_expression(self):
        # RTL: (v + (1 <<< (s-1))) >>>> s  with arithmetic shift = floor
        import random
        rng = random.Random(42)
        for _ in range(2000):
            v = rng.randint(-(1 << 40), 1 << 40)
            s = rng.randint(1, 20)
            expected = (v + (1 << (s - 1))) >> s   # Python >> is floor
            self.assertEqual(round_shift(v, s), expected, (v, s))


class TestSaturate(unittest.TestCase):
    def test_in_range(self):
        self.assertEqual(saturate(0, 8), 0)
        self.assertEqual(saturate(127, 8), 127)
        self.assertEqual(saturate(-128, 8), -128)

    def test_clamps(self):
        self.assertEqual(saturate(128, 8), 127)
        self.assertEqual(saturate(-129, 8), -128)
        self.assertEqual(saturate(1 << 30, 16), 32767)
        self.assertEqual(saturate(-(1 << 30), 16), -32768)


class TestKaratsuba(unittest.TestCase):
    def test_equals_direct_product(self):
        import random
        rng = random.Random(7)
        for _ in range(5000):
            a, b, c, d = (rng.randint(-(1 << 60), 1 << 60) for _ in range(4))
            re, im = complex_multiply_karatsuba(a, b, c, d)
            self.assertEqual(re, a * c - b * d)
            self.assertEqual(im, a * d + b * c)


class TestQuantizeOutput(unittest.TestCase):
    def test_same_format_passthrough_saturation_only(self):
        self.assertEqual(quantize_output(100, -100, 2, 16, 2), (100, -100))
        self.assertEqual(quantize_output(1 << 20, 0, 2, 8, 2), (127, 0))

    def test_shrinking_rounds(self):
        # frac 4 -> frac 2: divide by 4 with round-half-up
        re, im = quantize_output(6, -6, 4, 16, 2)
        self.assertEqual(re, 2)    # 1.5 -> 2
        self.assertEqual(im, -1)   # -1.5 -> -1 (half toward +inf)

    def test_growing_exact(self):
        re, im = quantize_output(3, -2, 0, 16, 3)
        self.assertEqual(re, 24)
        self.assertEqual(im, -16)

    def test_saturates_after_rounding(self):
        re, im = quantize_output(511, 0, 0, 8, 0)
        self.assertEqual(re, 127)


class TestWrap(unittest.TestCase):
    def test_wrap(self):
        self.assertEqual(wrap(128, 8), -128)
        self.assertEqual(wrap(127, 8), 127)
        self.assertEqual(wrap(-129, 8), 127)
        self.assertEqual(wrap(0, 8), 0)


if __name__ == "__main__":
    unittest.main()
