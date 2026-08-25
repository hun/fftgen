"""SSR golden model vs batch reference (bit-exact, fixed-point)."""
import random
import unittest

from config import FFTConfig
import dataclasses
from golden import fft_fixed_batch
from golden import _bitrev


def _batch1(cfg, frame):
    """Natural-bin-order reference (fft_fixed_batch returns DIF bitrev
    slot order -- undo it)."""
    br = fft_fixed_batch(frame, dataclasses.replace(cfg, ssr=1))
    n = len(frame).bit_length() - 1
    return [br[_bitrev(k, n)] for k in range(len(frame))]
from golden_ssr import SSRGoldenModel, ssr_emission_to_native


def _check(cfg, num_frames=3, seed=1):
    N, R = cfg.num_points, cfg.ssr
    M = N // R
    rng = random.Random(seed)
    # filler frames push the pipeline past its latency; the first valid
    # emission block is frame-aligned (latency % M == 0) and corresponds
    # to input frame d = latency // M
    m0 = SSRGoldenModel(cfg)
    pad = (m0.latency + M) // M + 3
    frames = [[(rng.randint(-2 ** 12, 2 ** 12 - 1),
                rng.randint(-2 ** 12, 2 ** 12 - 1)) for _ in range(N)]
              for _ in range(num_frames + pad)]

    samples = [s for fr in frames for s in fr]
    outs = m0.process_stream(samples)

    # drop any incomplete tail block, keep the frame-aligned prefix
    outs = outs[:(len(outs) // N) * N]
    n_blocks = len(outs) // N
    assert n_blocks >= num_frames, \
        f"expected >= {num_frames} complete blocks, got {n_blocks}"

    perm = ssr_emission_to_native(N, R)
    # locate the frame offset of block 0 (robust to sync details), then
    # verify every REAL block (drain-zero frames have no expectation)
    # The SSR composition quantizes TWICE (each lane engine snaps to
    # output_width, then the crossbar re-quantizes after its own rounding
    # shift); the monolithic batch quantizes once. Lane-input rounding
    # (+/-0.5 LSB) passes through the R-point DFT (gain <= R worst case),
    # plus the final rounding: bound = R/2 + 1 LSB.
    tol = R // 2 + 1

    def close(a, b):
        return all(abs(x - y) <= tol for x, y in zip(a, b))

    def block_ok(b, d):
        exp = _batch1(cfg, frames[b + d])
        return all(close(outs[b * N + e][:2], exp[perm[e]])
                   for e in range(N))
    d0 = next((d for d in range(pad + 3) if block_ok(0, d)), None)
    if d0 is None:
        raise AssertionError(f"{cfg}: no frame offset matches block 0")
    n_check = min(n_blocks, len(frames) - d0)
    assert n_check >= num_frames, \
        f"only {n_check} complete real blocks (need {num_frames})"
    for b in range(n_check):
        if not block_ok(b, d0):
            raise AssertionError(
                f"{cfg} block {b} mismatches frame {b + d0}")


class TestSSRvsBatch(unittest.TestCase):
    def test_r2_sizes(self):
        for N in (4, 8, 16, 32, 64):
            with self.subTest(N=N):
                _check(FFTConfig(num_points=N, ssr=2,
                                output_order='native'), seed=N)

    def test_r4_sizes(self):
        for N in (8, 16, 32):
            with self.subTest(N=N):
                _check(FFTConfig(num_points=N, ssr=4,
                                output_order='native'), seed=N + 100)

    def test_r8(self):
        for N in (16, 32):
            with self.subTest(N=N):
                _check(FFTConfig(num_points=N, ssr=8,
                                output_order='native'), seed=N + 200)

    def test_inverse_r2(self):
        for N in (8, 32):
            with self.subTest(N=N):
                _check(FFTConfig(num_points=N, ssr=2, inverse=True,
                                output_order='native'), seed=N + 300)

    def test_width_variant(self):
        with self.subTest():
            _check(FFTConfig(num_points=16, ssr=2, sample_width=14,
                             output_width=18,
                             output_order='native'), seed=7)


if __name__ == "__main__":
    unittest.main()
