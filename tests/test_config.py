import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig, is_power_of_two, VALID_ORDERS


class TestIsPowerOfTwo(unittest.TestCase):
    def test_values(self):
        for n in (1, 2, 4, 1024, 1 << 20):
            self.assertTrue(is_power_of_two(n), n)
        for n in (0, 3, 6, 12, 1000, -4):
            self.assertFalse(is_power_of_two(n), n)


class TestFFTConfigValid(unittest.TestCase):
    def test_minimal(self):
        cfg = FFTConfig(num_points=16)
        self.assertEqual(cfg.num_stages, 4)
        self.assertEqual(cfg.shifts, (1, 1, 1, 1))
        self.assertTrue(cfg.scaling_guaranteed)
        self.assertEqual(cfg.output_width, 16)
        self.assertEqual(cfg.output_decimal, 0)
        self.assertEqual(cfg.twiddle_decimal, 17)
        self.assertFalse(cfg.inverse)
        self.assertFalse(cfg.is_dit)

    def test_explicit_everything(self):
        cfg = FFTConfig(num_points=64, inverse=True, ssr=4,
                        input_order="bitreversed", output_order="native",
                        sample_width=25, sample_decimal=2,
                        output_width=20, output_decimal=1,
                        twiddle_width=16, twiddle_decimal=14,
                        scaling=[0, 1, 2, 1, 1, 1])
        self.assertTrue(cfg.is_dit)
        self.assertEqual(cfg.shifts, (0, 1, 2, 1, 1, 1))
        self.assertTrue(cfg.scaling_guaranteed)  # sum = 6 >= log2(64)

    def test_n1_edge(self):
        cfg = FFTConfig(num_points=2)
        self.assertEqual(cfg.num_stages, 1)
        self.assertEqual(cfg.shifts, (1,))


class TestFFTConfigInvalid(unittest.TestCase):
    def assert_invalid(self, **kwargs):
        with self.assertRaises(ValueError):
            FFTConfig(**kwargs)

    def test_num_points(self):
        self.assert_invalid(num_points=0)
        self.assert_invalid(num_points=1)      # >= 2 required
        self.assert_invalid(num_points=12)
        self.assert_invalid(num_points=-8)

    def test_ssr(self):
        for bad in (3, 16, 0, -2):             # 16 not in VALID_SSR for v1
            self.assert_invalid(num_points=64, ssr=bad)

    def test_ssr_divide(self):
        FFTConfig(num_points=32, ssr=8)      # 8 | 32: fine
        with self.assertRaises(ValueError):
            FFTConfig(num_points=4, ssr=8)   # 8 does not divide 4

    def test_orders(self):
        self.assert_invalid(num_points=8, input_order="reversed")
        self.assert_invalid(num_points=8, output_order="")

    def test_widths(self):
        self.assert_invalid(num_points=8, sample_width=1)
        self.assert_invalid(num_points=8, twiddle_width=0)
        self.assert_invalid(num_points=8, sample_width=8, sample_decimal=8)
        self.assert_invalid(num_points=8, twiddle_width=18, twiddle_decimal=18)

    def test_scaling(self):
        self.assert_invalid(num_points=8, scaling=(1, 1))       # too short
        self.assert_invalid(num_points=8, scaling=(1, 1, 1, 1)) # too long
        self.assert_invalid(num_points=8, scaling=(1, 1, 3))    # out of range
        self.assert_invalid(num_points=8, scaling=(1, 1, -1))


class TestSSRDivide(unittest.TestCase):
    def test_valid_combinations(self):
        for n, r in ((8, 1), (8, 2), (8, 4), (8, 8),
                     (1024, 8), (4, 2), (2, 1)):
            cfg = FFTConfig(num_points=n, ssr=r)
            self.assertEqual(cfg.ssr, r)


class TestStageMode(unittest.TestCase):
    def test_default_is_r2(self):
        self.assertEqual(FFTConfig(num_points=8).stage_mode, "r2")

    def test_r22_valid(self):
        for N in (2, 4, 8, 16, 2048):
            for inv in (False, True):
                cfg = FFTConfig(num_points=N, inverse=inv,
                                stage_mode="r22")
                self.assertTrue(cfg.is_r22)
                self.assertIn("mode=r22", repr(cfg))

    def test_r22_ssr_orders(self):
        # SSR r22 shares the SSR v1 native -> native contract
        for r in (2, 4, 8):
            cfg = FFTConfig(num_points=16 * r, ssr=r, stage_mode="r22",
                            output_order="native")
            self.assertTrue(cfg.is_r22)
        with self.assertRaises(ValueError):
            FFTConfig(num_points=32, ssr=2, stage_mode="r22")  # bitrev out
        with self.assertRaises(ValueError):
            FFTConfig(num_points=32, ssr=2, stage_mode="r22",
                      output_order="native", input_order="bitreversed")

    def test_r22_rejects_other_orders(self):
        for kw in (dict(input_order="bitreversed"),
                   dict(output_order="native"),
                   dict(input_order="bitreversed", output_order="native")):
            with self.subTest(**kw):
                with self.assertRaises(ValueError):
                    FFTConfig(num_points=16, stage_mode="r22", **kw)

    def test_bad_stage_mode(self):
        for bad in ("r4", ""):
            with self.assertRaises(ValueError):
                FFTConfig(num_points=8, stage_mode=bad)

    def test_r2_all_orders_still_ok(self):
        for io in VALID_ORDERS:
            for oo in VALID_ORDERS:
                FFTConfig(num_points=8, input_order=io, output_order=oo)


if __name__ == "__main__":
    unittest.main()
