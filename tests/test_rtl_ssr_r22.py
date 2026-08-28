"""P7 step 5: SSR RTL bit-exactness for r22 (R = 2, 4, 8).

Lanes are the verified R=1 r22 production core (fft_sdf_r22,
REORDER_OUT=1 -- separately proven cycle-exact vs the model lane);
fft_cross is the r2 crossbar unchanged. Comparison uses the SSR
documented tolerance (R/2 + 1 LSB after word-offset alignment),
identical to the r2 flow. Requires Verilator; auto-skip otherwise.
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig
from fft_gen import generate_ssr

HAVE_VERILATOR = shutil.which("verilator") is not None


@unittest.skipUnless(HAVE_VERILATOR, "verilator not available")
class TestSSR22Rtl(unittest.TestCase):
    def _check(self, cfg, num_frames=6, seed=5):
        outdir = (f"build/ssr22/N{cfg.num_points}_R{cfg.ssr}"
                  f"{'_inv' if cfg.inverse else ''}")
        r = generate_ssr(cfg, outdir, num_frames=num_frames, seed=seed)
        self.assertEqual(r["rc"], 0,
                         f"{cfg}\n{r.get('log', '')[-500:]}\n"
                         f"first_bad={r.get('first_bad')}")

    def _cfg(self, N, R, inv=False):
        return FFTConfig(num_points=N, ssr=R, inverse=inv,
                         output_order="native", stage_mode="r22")

    def test_r2_sizes(self):
        for N in (8, 16, 32, 64):
            with self.subTest(N=N):
                self._check(self._cfg(N, 2))

    def test_inverse_r2(self):
        for N in (8, 16, 32):
            with self.subTest(N=N):
                self._check(self._cfg(N, 2, inv=True))

    def test_r4_sizes(self):
        for N in (16, 32):
            with self.subTest(N=N):
                self._check(self._cfg(N, 4))

    def test_inverse_r4(self):
        for N in (16, 32):
            with self.subTest(N=N):
                self._check(self._cfg(N, 4, inv=True))

    def test_r8_sizes(self):
        # N=16 R=8 exercises the M=2 lane (leftover-only r22 core)
        for N in (16, 32):
            with self.subTest(N=N):
                self._check(self._cfg(N, 8))

    def test_inverse_r8(self):
        for N in (16, 32):
            with self.subTest(N=N):
                self._check(self._cfg(N, 8, inv=True))


if __name__ == "__main__":
    unittest.main()
