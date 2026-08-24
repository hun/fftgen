"""P2: bit-exact RTL verification under Verilator.

Runs the generated core through fft_gen.generate() against the golden
model's expected vectors. Skipped automatically when verilator is absent.
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
class TestRTLBitExact(unittest.TestCase):
    def _check(self, cfg, num_frames=2, **kw):
        outdir = f"build/test_rtl/{cfg.num_points}_{int(cfg.inverse)}_{kw.get('freeze','dense')}_{id(cfg)&0xfff}"
        r = generate(cfg, outdir, num_frames=num_frames, seed=7, **kw)
        self.assertEqual(r["rc"], 0, f"{cfg}\n{r.get('log','')[:500]}")
        if r.get("first_bad") is not None:
            self.fail(f"{cfg}: first mismatch {r['first_bad']}")

    def test_sizes(self):
        for N in (2, 4, 8, 16, 32, 64, 128):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N))

    def test_inverse(self):
        for N in (8, 16, 128):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, inverse=True))

    def test_width_variants(self):
        self._check(FFTConfig(num_points=16, sample_width=8, twiddle_width=8))
        self._check(FFTConfig(num_points=16, sample_width=25, output_width=20))
        self._check(FFTConfig(num_points=16, sample_width=12, output_width=20,
                              sample_decimal=3, output_decimal=2))
        self._check(FFTConfig(num_points=16, twiddle_width=10,
                              twiddle_decimal=8))

    def test_scaling_schedules(self):
        self._check(FFTConfig(num_points=8, scaling=(0, 0, 0), output_width=24))
        self._check(FFTConfig(num_points=8, scaling=(2, 0, 1), output_width=24))
        self._check(FFTConfig(num_points=8, scaling=(2, 2, 2), output_width=24))

    def test_freeze_masks(self):
        for style in ("periodic", "pseudo", "bursty"):
            with self.subTest(style=style):
                self._check(FFTConfig(num_points=16), freeze=style)
        self._check(FFTConfig(num_points=32), freeze="pseudo")

    def test_multi_frame(self):
        self._check(FFTConfig(num_points=16), num_frames=4)


if __name__ == "__main__":
    unittest.main()
