"""P8 step 1: SSR corner orders in the golden model (doc/plan_p8_ssr_orders.md).

Two independent checks on the R=2 radix-2^2 `native -> bitreversed` emission:

1. EXACT EQUIVALENCE -- the corner-order model must produce, per bin index,
   bit-for-bit the same values AND the same tuser/tlast markers as the
   already-verified native-order model. This is the load-bearing test: the
   only thing that differs between the two configurations is *which clock*
   a given bin is emitted on, so any phase error in the bit-reversed bin
   index shows up immediately as a value mismatch. (Beware the vacuous pass:
   a frame window that never drained is all zeros and matches everything,
   hence the explicit "real frames emitted" counts asserted below.)

2. BATCH REFERENCE -- the emission against the monolithic fixed-point batch
   model, through the bitrev_N slot permutation, within the SSR
   double-quantization tolerance the native tests already use.

Plus the contract guards: the P8 subset must be constructible, everything
outside it must raise ValueError from config (NOT an assert from the
generator -- the r2 arch used to leak corner orders through).
"""
import random
import unittest

from config import FFTConfig, SSR_CORNER_ORDERS
from golden import fft_fixed_batch, _bitrev
from golden_ssr import (SSRGoldenModel, SSRCornerInverseModel,
                        ssr_emission_perm)


def _frames(N, count, seed=7, bits=12):
    rng = random.Random(seed)
    return [[(rng.randint(-2 ** bits, 2 ** bits - 1),
              rng.randint(-2 ** bits, 2 ** bits - 1)) for _ in range(N)]
            for _ in range(count)]


def _emitting_frames(cfg, real, lead, tail):
    """Run `cfg` on lead zero frames + `real` + tail zero frames and return
    the per-bin content of every NON-ZERO emission frame:
    {bin_index: ((re, im), (tuser, tlast))}.

    Locating frames by their SOF marker makes this free of latency
    bookkeeping, and keeping only non-zero windows makes the comparison
    non-vacuous."""
    m = SSRGoldenModel(cfg, arch=cfg.stage_mode)
    allf = [[(0, 0)] * cfg.num_points] * lead + real + \
           [[(0, 0)] * cfg.num_points] * tail
    samples = [s for fr in allf for s in fr]
    markers = []
    for _ in allf:
        markers += [(1, 0)] + [(0, 0)] * (cfg.num_points - 2) + [(0, 1)]
    outs = m.process_stream(samples, markers=markers)
    perm = ssr_emission_perm(cfg.num_points, cfg.ssr, cfg.output_order)
    sofs = [i for i, o in enumerate(outs) if o[2] == 1]
    wins = [{perm[e]: (outs[s + e][:2], (outs[s + e][2], outs[s + e][3]))
             for e in range(cfg.num_points)}
            for s in sofs if s + cfg.num_points <= len(outs)]
    return [w for w in wins
            if any(v[0] != (0, 0) for v in w.values())]


def _cfg(N, out_order, inv=False):
    return FFTConfig(num_points=N, ssr=2, output_order=out_order,
                     stage_mode="r22", inverse=inv)


class TestSSRCornerModel(unittest.TestCase):
    """The P8 forward corner order vs the verified native configuration."""

    def test_exact_equivalence_with_native(self):
        for N in (16, 64, 256):
            with self.subTest(N=N):
                real = _frames(N, 3)
                nat = _emitting_frames(_cfg(N, "native"), real, 20, 20)
                brv = _emitting_frames(_cfg(N, "bitreversed"), real, 20, 20)
                self.assertEqual(len(nat), 3, f"{N}: native frames emitted")
                self.assertEqual(len(brv), 3, f"{N}: bitrev frames emitted")
                self.assertEqual(nat, brv,
                                 f"{N}: corner order != bitrev perm of native")

    def test_latency_drops_by_M(self):
        # the corner order removes the lane reorder buffer entirely
        for N in (16, 64, 256, 2048):
            with self.subTest(N=N):
                d = (SSRGoldenModel(_cfg(N, "native"), arch="r22").latency
                     - SSRGoldenModel(_cfg(N, "bitreversed"),
                                      arch="r22").latency)
                self.assertEqual(d, N // 2, f"{N}: latency delta != M")

    def test_markers_land_on_bin_zero_and_last(self):
        # 0 and N-1 are bitrev fixed points, so SOF/EOF keep their slots
        for N in (16, 64, 256):
            with self.subTest(N=N):
                brv = _emitting_frames(_cfg(N, "bitreversed"), _frames(N, 1),
                                       20, 20)[0]
                self.assertEqual(brv[0][1], (1, 0), "SOF not on bin 0")
                self.assertEqual(brv[N - 1][1], (0, 1), "EOF not on bin N-1")

    def test_matches_batch_reference(self):
        for N in (16, 64, 128):
            with self.subTest(N=N):
                cfg = _cfg(N, "bitreversed")
                real = _frames(N, 3)
                wins = _emitting_frames(cfg, real, 20, 20)
                self.assertEqual(len(wins), 3)
                ref_cfg = FFTConfig(num_points=N, ssr=1,
                                    output_order="bitreversed",
                                    stage_mode="r22")
                n = N.bit_length() - 1
                tol = cfg.ssr // 2 + 1
                for b, w in enumerate(wins):
                    br = fft_fixed_batch(real[b], ref_cfg)
                    for k in range(N):
                        want = br[_bitrev(k, n)]      # undo DIF slot order
                        got = w[k][0]
                        self.assertTrue(
                            all(abs(x - y) <= tol for x, y in zip(got, want)),
                            f"N={N} bin {k}: {got} vs {want}")


class TestSSROrderContract(unittest.TestCase):
    """What the config layer promises, and what it must refuse."""

    def test_subset_is_constructible(self):
        for (mode, r, i_o, o_o, inv) in SSR_CORNER_ORDERS:
            with self.subTest(subset=(mode, r, i_o, o_o, inv)):
                cfg = FFTConfig(num_points=64, inverse=inv, ssr=r,
                                input_order=i_o, output_order=o_o,
                                stage_mode=mode)
                self.assertTrue(cfg.ssr_corner_supported())
                SSRGoldenModel(cfg, arch=mode)     # must not raise

    def test_native_native_still_fine(self):
        SSRGoldenModel(FFTConfig(num_points=64, ssr=2, output_order="native",
                                 stage_mode="r22"), arch="r22")

    def test_everything_else_raises_value_error(self):
        # R=4/8 corner orders need bitrev_R to be a real permutation; the
        # inverse corner needs an r22 DIT lane. None of these may reach the
        # generator, and none may raise anything as quiet as an assert.
        bad = [
            dict(num_points=64, ssr=4, output_order="bitreversed",
                 stage_mode="r22"),                      # R=4 corner
            dict(num_points=64, ssr=8, output_order="bitreversed",
                 stage_mode="r22"),                      # R=8 corner
            dict(num_points=64, ssr=2, input_order="bitreversed",
                 output_order="native", stage_mode="r22"),   # inverse corner
            dict(num_points=64, ssr=2, output_order="bitreversed",
                 stage_mode="r22", inverse=True),            # inv order combo
            dict(num_points=64, ssr=2, output_order="bitreversed",
                 stage_mode="r2"),                       # r2 arch: the leak
            dict(num_points=64, ssr=2, input_order="bitreversed",
                 stage_mode="r2"),                       # r2 arch: the leak
        ]
        for kw in bad:
            with self.subTest(**kw):
                with self.assertRaises(ValueError) as cm:
                    FFTConfig(**kw)
                self.assertNotIsInstance(cm.exception, AssertionError)


class TestSSRCornerInverseModel(unittest.TestCase):
    """P8 step 4a: the corner-order IFFT model (bitrev -> native, R=2).

    Two properties, both exact up to rounding:

    1. Numpy identity -- feeding bin X[bitrev_N(e)] at slot e must produce
       x[e] (the IDFT of FFT(x) is x). Any phase/twiddle/index error shows
       as a value blow-up, not a rounding delta.

    2. ROUND TRIP through the verified corner FFT model -- the customer's
       actual chain: FFT(corner) -> IFFT(corner) must return the input
       scaled by a power of two, with the frame markers intact. This is the
       property the TX/RX fast-convolution pair relies on.
    """

    def _inv(self, N):
        cfg = FFTConfig(num_points=N, ssr=2, inverse=True,
                        input_order="bitreversed", output_order="native",
                        stage_mode="r22")
        return cfg, SSRCornerInverseModel(cfg, arch="r22")

    def _numpy_check(self, N):
        import numpy as np
        cfg, m = self._inv(N)
        rng = random.Random(3)
        x = np.array([rng.randint(-2 ** 12, 2 ** 12 - 1) for _ in range(N)],
                     dtype=float)
        X = np.fft.fft(x)
        # the FINAL spectrum as a fixed-point core actually carries it: the
        # forward shift schedule divides by 2^log2(N), so feed X/N and expect
        # x/N -- NOT the raw FFT (its O(N)-larger values would saturate the
        # 16-bit lane input and tell us nothing).
        k = N.bit_length() - 1
        L = N.bit_length() - 1
        br = _bitrev
        samples = [(int(round(X[br(e, L)].real / (2 ** k))),
                    int(round(X[br(e, L)].imag / (2 ** k))))
                   for e in range(N)]
        mk = [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]
        pad = 40
        samples2 = [(0, 0)] * (pad * N) + samples + [(0, 0)] * (20 * N)
        mks = []
        for _ in range(pad):
            mks += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]
        mks += mk
        for _ in range(20):
            mks += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]
        outs = m.process_stream(samples2, markers=mks)
        sofs = [i for i, o in enumerate(outs) if o[2] == 1]
        w = next((outs[s:s + N] for s in sofs
                  if s + N <= len(outs)
                  and any(o[0] != 0 or o[1] != 0
                          for o in outs[s:s + N])), None)
        self.assertIsNotNone(w, "no real frame emitted")
        self.assertEqual(w[0][2], 1, "SOF not on first slot")
        self.assertEqual(w[-1][3], 1, "EOF not on last slot")
        worst = max(abs(o[0] - x[i] / (2 ** k)) for i, o in enumerate(w))
        self.assertLessEqual(worst, 3,
                             f"N={N}: IDFT(X/N) != x/N, worst |d|={worst}")

    def test_numpy_identity(self):
        for N in (16, 64, 128):
            with self.subTest(N=N):
                self._numpy_check(N)

    def test_round_trip_through_corner_fft(self):
        # corner FFT model (verified) -> corner IFFT model = scaled x
        for N in (16, 64):
            with self.subTest(N=N):
                running = self._round_trip(N)
                self.assertTrue(running, f"N={N}: round trip broke")

    def _round_trip(self, N):
        real = _frames(N, 2)
        lead = tail = 20
        fwd = FFTConfig(num_points=N, ssr=2, output_order="bitreversed",
                        stage_mode="r22")
        fm = SSRGoldenModel(fwd, arch="r22")
        allf = [[(0, 0)] * N] * lead + real + [[(0, 0)] * N] * tail
        samples = [s for fr in allf for s in fr]
        markers = []
        for _ in allf:
            markers += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]
        fat = fm.process_stream(samples, markers=markers)
        # slice the real frames out of the FFT emission (non-zero windows
        # located by SOF), feed slot-for-slot into the IFFT
        sofs = [i for i, o in enumerate(fat) if o[2] == 1]
        wins = [fat[s:s + N] for s in sofs if s + N <= len(fat)]
        wins = [w for w in wins if any(o[0] != 0 or o[1] != 0 for o in w)]
        self.assertEqual(len(wins), 2, f"N={N}: FFT real frames")
        cfg, inv = self._inv(N)
        lead2 = tail2 = 20
        allf2 = [[(0, 0)] * N] * lead2 + [[(o[0], o[1]) for o in w]
                                          for w in wins] + [[(0, 0)] * N] * tail2
        samples2 = [s for fr in allf2 for s in fr]
        markers2 = []
        for _ in allf2:
            markers2 += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]
        iat = inv.process_stream(samples2, markers=markers2)
        sofs2 = [i for i, o in enumerate(iat) if o[2] == 1]
        wins2 = [iat[s:s + N] for s in sofs2 if s + N <= len(iat)]
        wins2 = [w for w in wins2 if any(o[0] != 0 or o[1] != 0 for o in w)]
        if len(wins2) < 2:
            return False
        # FFT model shifts by log2(N) per stage pair schedule; find the
        # power-of-two scale that recovers the input
        got = wins2[-2:] if len(wins2) >= 2 else wins2
        for sh in range(0, 9):
            sc = 2 ** sh
            errs = [max(abs(o[0] - (s[0] / sc)), abs(o[1] - (s[1] / sc)))
                    for w, r in zip(got, real)
                    for o, s in zip(w, r)]
            if max(errs) <= 2.5:
                return True
        return False


if __name__ == "__main__":
    unittest.main()
