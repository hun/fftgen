"""P7 step 4: bit-exact RTL verification of the production R2^2 core.

Runs fft_gen.generate() with stage_mode='r22' (generic fft_top_r22 /
fft_sdf_r22 / fft_stage_r22 under Verilator) against the re-pinned
R22SDFGoldenModel contract -- values and tuser/tlast markers, plus the
ce/tvalid freeze suites. Mirrors tests/test_rtl_verilator.py (P2) for
the v1-verified r22 corner (DIF, native -> bitreversed, R = 1; see
config.py stage_mode guards). Skipped automatically without Verilator.
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig
from fft_gen import generate

HAVE_VERILATOR = shutil.which("verilator") is not None


@unittest.skipUnless(HAVE_VERILATOR, "verilator not available")
class TestRTL22BitExact(unittest.TestCase):
    def _check(self, cfg, num_frames=2, **kw):
        self.assertTrue(cfg.is_r22)
        outdir = (f"build/test_rtl_r22/{cfg.num_points}_{int(cfg.inverse)}"
                  f"_{kw.get('freeze', 'dense')}_{id(cfg) & 0xfff}")
        r = generate(cfg, outdir, num_frames=num_frames, seed=7, **kw)
        self.assertEqual(r["rc"], 0, f"{cfg}\n{r.get('log', '')[:500]}")
        if r.get("first_bad") is not None:
            self.fail(f"{cfg}: first mismatch {r['first_bad']}")

    def test_sizes(self):
        # odd AND even stage counts (leftover parity), the last-pair
        # D=1 collapse and the N=2 leftover-only core
        for N in (2, 4, 8, 16, 32, 64, 128):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, stage_mode="r22"))

    def test_inverse(self):
        for N in (8, 16, 128):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, inverse=True,
                                      stage_mode="r22"))

    def test_width_variants(self):
        self._check(FFTConfig(num_points=16, stage_mode="r22",
                              sample_width=8, twiddle_width=8))
        self._check(FFTConfig(num_points=16, stage_mode="r22",
                              sample_width=25, output_width=20))
        self._check(FFTConfig(num_points=16, stage_mode="r22",
                              sample_width=12, output_width=20,
                              sample_decimal=3, output_decimal=2))
        self._check(FFTConfig(num_points=16, stage_mode="r22",
                              twiddle_width=10, twiddle_decimal=8))

    def test_scaling_schedules(self):
        self._check(FFTConfig(num_points=8, stage_mode="r22",
                              scaling=(0, 0, 0), output_width=24))
        self._check(FFTConfig(num_points=8, stage_mode="r22",
                              scaling=(2, 0, 1), output_width=24))
        self._check(FFTConfig(num_points=16, stage_mode="r22",
                              scaling=(2, 2, 2, 0), output_width=24))

    def test_freeze_masks(self):
        for style in ("periodic", "pseudo", "bursty"):
            with self.subTest(style=style):
                self._check(FFTConfig(num_points=16, stage_mode="r22"),
                            freeze=style)
        self._check(FFTConfig(num_points=32, stage_mode="r22"),
                    freeze="pseudo")

    def test_multi_frame(self):
        self._check(FFTConfig(num_points=16, stage_mode="r22"),
                    num_frames=4)

    def test_large_n_smoke(self):
        # chain depth / marker window at sweep-relevant sizes (the
        # spike rtl_check_prod covers N=2048; keep the suite bounded)
        self._check(FFTConfig(num_points=256, stage_mode="r22"),
                    num_frames=1)


if __name__ == "__main__":
    unittest.main()
