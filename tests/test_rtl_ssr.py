"""P4: SSR RTL bit-exactness (Verilator), R=2 first."""
import unittest

from config import FFTConfig
from fft_gen import generate_ssr

class TestSSRRtl(unittest.TestCase):
    def _check(self, cfg, num_frames=6, seed=5):
        outdir = (f"build/ssr/N{cfg.num_points}_R{cfg.ssr}"
                  f"{'_inv' if cfg.inverse else ''}"
                  f"_sw{cfg.sample_width}ow{cfg.output_width}")
        r = generate_ssr(cfg, outdir, num_frames=num_frames, seed=seed)
        self.assertEqual(r["rc"], 0,
                         f"{cfg}\n{r.get('log', '')[-500:]}\n"
                         f"first_bad={r.get('first_bad')}")

    def test_r2_sizes(self):
        for N in (8, 16, 32):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, ssr=2,
                                      output_order="native"))

    def test_inverse_r2(self):
        for N in (8, 16):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, ssr=2, inverse=True,
                                      output_order="native"))

    def test_r4_sizes(self):
        for N in (16, 32):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, ssr=4,
                                      output_order="native"))

    def test_inverse_r4(self):
        for N in (16, 32):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, ssr=4, inverse=True,
                                      output_order="native"))

    def test_r8_sizes(self):
        for N in (16, 32):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, ssr=8,
                                      output_order="native"))

    def test_inverse_r8(self):
        for N in (16, 32):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N, ssr=8, inverse=True,
                                      output_order="native"))


if __name__ == "__main__":
    unittest.main()
