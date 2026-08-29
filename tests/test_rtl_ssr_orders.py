"""P8 step 2: SSR corner-order RTL bit-exactness (R = 2, radix-2^2).

Two checks, deliberately in this order of strength:

1. FLOW CHECK -- ``generate_ssr`` against the golden model for the corner
   order, at every size the SSR suites already use. Since P8 this includes a
   POSITIONAL marker comparison (the old flow only counted one SOF/EOF per
   frame group, which is blind to skew -- see the commit that fixed the SSR
   marker pipeline).

2. EXACT PERMUTATION vs the VERIFIED NATIVE CORE -- the real proof. Both
   configurations are built from the identical stimulus (same seed AND same
   pad_frames, so the same input frames), and the only difference in the RTL
   is the lane reorder parameter plus the crossbar's WN row index. So the
   corner-order stream must be, bin for bin and BIT FOR BIT, the native
   stream's frame contents read through the bitrev_N slot permutation: no
   tolerance, no rounding argument, no dependence on the model.

   Frames are located by SOF and matched over a small shift range: the two
   cores differ in latency by exactly M clocks = one frame, so the k-th
   emitted frame of one is the (k+1)-th of the other. The search is
   reported, not assumed -- a diagonal-with-one-shift result is exactly what
   the design predicts, and anything else (a fuzzy diagonal, partial
   agreement) would mean a mis-paired WN row.

Requires Verilator; auto-skips otherwise.
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig
from fft_gen import generate_ssr

HAVE_VERILATOR = shutil.which("verilator") is not None


def _load(path):
    with open(path) as f:
        return [tuple(int(x) for x in ln.split()) for ln in f if ln.strip()]


def _sof_windows(stream, N, perm):
    """[{bin_index: (re, im)}] per SOF-anchored frame window."""
    sofs = [i for i, o in enumerate(stream) if o[2] == 1]
    return [{perm[e]: stream[s + e][:2] for e in range(N)}
            for s in sofs if s + N <= len(stream)]


def _perm(N, R, out_order):
    M = N // R
    if out_order == "native":
        return [(e % R) * M + (e // R) for e in range(N)]
    return [int(format(e, f"0{N.bit_length() - 1}b")[::-1], 2) for e in range(N)]


@unittest.skipUnless(HAVE_VERILATOR, "verilator not available")
class TestSSROrderRtl(unittest.TestCase):
    def _gen(self, N, out_order, tag, pad_frames=None, num_frames=4):
        cfg = FFTConfig(num_points=N, ssr=2, output_order=out_order,
                        stage_mode="r22")
        outdir = f"build/p8_rtl/{tag}_N{N}_{out_order}"
        r = generate_ssr(cfg, outdir, num_frames=num_frames, seed=5,
                         pad_frames=pad_frames)
        self.assertEqual(r["rc"], 0,
                         f"{cfg}\n{r.get('log', '')[-600:]}\n"
                         f"first_bad={r.get('first_bad')} "
                         f"marker_mismatches={r.get('marker_mismatches')}")
        return outdir, r

    def test_corner_flow_bit_exact(self):
        # rc==0 now implies positionally exact tuser/tlast as well
        for N in (8, 16, 32, 64):
            with self.subTest(N=N):
                self._gen(N, "bitreversed", "flow")

    def test_exact_permutation_of_verified_native_core(self):
        for N in (16, 64):
            with self.subTest(N=N):
                R = 2
                # identical pads -> identical stimulus for both builds
                dn, rn = self._gen(N, "native", "perm", pad_frames=8)
                db, rb = self._gen(N, "bitreversed", "perm", pad_frames=8)
                A = _sof_windows(_load(f"{dn}/actual.txt"), N, _perm(N, R, "native"))
                B = _sof_windows(_load(f"{db}/actual.txt"), N, _perm(N, R, "bitreversed"))
                self.assertGreaterEqual(len(A), 3, f"{N}: too few native frames")
                self.assertGreaterEqual(len(B), 3, f"{N}: too few bitrev frames")
                # exactly one shift must produce an all-equal overlap
                good = []
                for sh in range(-2, 3):
                    prs = [(i, i + sh) for i in range(len(A))
                           if 0 <= i + sh < len(B)]
                    if len(prs) < 3:
                        continue
                    bad = sum(1 for i, j in prs
                              for k in A[i] if A[i][k] != B[j][k])
                    if bad == 0:
                        good.append((sh, prs))
                self.assertEqual(len(good), 1,
                                 f"N={N}: expected exactly one aligning shift, "
                                 f"got {good} (a fuzzy match means a mis-paired "
                                 f"WN row, not a reordering)")
                sh, prs = good[0]
                self.assertGreaterEqual(len(prs), 3)
                # The two cores differ in latency by exactly M clocks = one
                # frame, so the emission grids may differ by at most one frame
                # index -- more than that means the comparison is not the
                # same data and the "exact" match would be meaningless.
                self.assertIn(sh, (0, 1),
                              f"N={N}: aligning shift {sh} is more than the "
                              f"one-frame latency delta -> suspicious")
                # the matched windows must carry distinct values, else a
                # permutation match could be vacuous
                self.assertEqual(len(set(A[prs[0][0]].values())), N,
                                 f"N={N}: frame values not distinct -> weak test")


@unittest.skipUnless(HAVE_VERILATOR, "verilator not available")
class TestSSRCornerInverseRtl(unittest.TestCase):
    """P8 step 4b: the corner-order IFFT RTL (bitrev -> native, R=2, r22).

    1. FLOW CHECK vs SSRCornerInverseModel at every suite size -- values
       BIT-EXACT (tol = 0 in the flow: the inverse has one quantization
       point, the wrapper's a1 quantize, mirrored exactly by the RTL) and
       tuser/tlast positionally exact.

    2. ROUND TRIP through the VERIFIED corner-FFT RTL -- the customer's
       chain: FFT(corner) output frame (read from the FFT build's
       actual.txt) feeds the IFFT build's stimulus verbatim, and the
       IFFT output must recover the FFT's input x scaled by 2^-log2(N)
       (both cores divide by N). This is the property the TX/RX
       fast-convolution pair relies on.
    """

    def _gen_inv(self, N, tag, num_frames=4):
        cfg = FFTConfig(num_points=N, ssr=2, inverse=True,
                        input_order="bitreversed", output_order="native",
                        stage_mode="r22")
        outdir = f"build/p8_rtl/{tag}_N{N}_inv"
        r = generate_ssr(cfg, outdir, num_frames=num_frames, seed=5)
        self.assertEqual(r["rc"], 0,
                         f"{cfg}\n{r.get('log', '')[-600:]}\n"
                         f"first_bad={r.get('first_bad')} "
                         f"marker_mismatches={r.get('marker_mismatches')}")
        return outdir, r

    def test_inverse_flow_bit_exact(self):
        # rc==0 now implies BIT-EXACT values (tol=0) AND positionally
        # exact tuser/tlast for this config
        for N in (8, 16, 32, 64):
            with self.subTest(N=N):
                self._gen_inv(N, "flow")

    def _round_trip_rtl(self, N):
        # 1. corner FFT RTL on a KNOWN stimulus: zero pads + x + zero
        # tail. Zero pads are essential: generate_ssr's default filler
        # frames are RANDOM, and the SSR emission is shifted one frame
        # (emitted frame j = transform of input frame j+1), so with random
        # pads every emitted frame is strong and the "frame that belongs to
        # x" is ambiguous. Zero pads leave exactly one strong window.
        import random
        rng = random.Random(11)
        x = [(rng.randint(-2 ** 12, 2 ** 12 - 1),
              rng.randint(-2 ** 12, 2 ** 12 - 1)) for _ in range(N)]

        def framed(frames):
            out = []
            for fr in frames:
                for j, (a, b) in enumerate(fr):
                    out.append((a, b, 1 if j == 0 else 0,
                                1 if j == N - 1 else 0))
            return out

        lead = tail = 20
        cfg_fwd = FFTConfig(num_points=N, ssr=2, output_order="bitreversed",
                            stage_mode="r22")
        fwd_stim = framed([[(0, 0)] * N] * lead + [x] + [[(0, 0)] * N] * tail)
        r = generate_ssr(cfg_fwd, f"build/p8_rtl/rt_N{N}_fwd",
                         stimulus=fwd_stim)
        self.assertEqual(r["rc"], 0,
                         f"{cfg_fwd}\n{r.get('log', '')[-600:]}\n"
                         f"first_bad={r.get('first_bad')} "
                         f"marker_mismatches={r.get('marker_mismatches')}")
        act = _load(f"{r['outdir']}/actual.txt")
        # 2. the FFT's emitted frame for x (the one strong window): X in
        # bitrev_N slot order -- the IFFT's stimulus, slot for slot
        sofs = [i for i, o in enumerate(act) if o[2] == 1]
        w = next((act[s:s + N] for s in sofs if s + N <= len(act)
                  and any(abs(o[0]) + abs(o[1]) > 100
                          for o in act[s:s + N])), None)
        if w is None:
            return False
        # 3. corner IFFT RTL on that frame (zero pads both sides)
        cfg_inv = FFTConfig(num_points=N, ssr=2, inverse=True,
                            input_order="bitreversed", output_order="native",
                            stage_mode="r22")
        inv_stim = framed([[(0, 0)] * N] * lead
                          + [[(o[0], o[1]) for o in w]]
                          + [[(0, 0)] * N] * tail)
        r = generate_ssr(cfg_inv, f"build/p8_rtl/rt_N{N}_inv",
                         stimulus=inv_stim)
        self.assertEqual(r["rc"], 0,
                         f"{cfg_inv}\n{r.get('log', '')[-600:]}\n"
                         f"first_bad={r.get('first_bad')} "
                         f"marker_mismatches={r.get('marker_mismatches')}")
        # 4. the IFFT's strong output frame = x / 2^log2(N) (both cores
        # divide by N)
        iat = _load(f"{r['outdir']}/actual.txt")
        sofs2 = [i for i, o in enumerate(iat) if o[2] == 1]
        w2 = next((iat[s:s + N] for s in sofs2 if s + N <= len(iat)
                   and any(abs(o[0]) + abs(o[1]) > 100
                           for o in iat[s:s + N])), None)
        if w2 is None:
            return False
        for sh in range(0, 9):
            sc = 2 ** sh
            errs = [max(abs(o[0] - (s[0] / sc)), abs(o[1] - (s[1] / sc)))
                    for o, s in zip(w2, x)]
            if max(errs) <= 4.0:
                # the chain must recover x at exactly 2^-log2(N), not some
                # other power of two (a wrong scale means a mis-set shift)
                return sh == N.bit_length() - 1
        return False

    def test_round_trip_through_corner_fft_rtl(self):
        for N in (16, 64):
            with self.subTest(N=N):
                self.assertTrue(self._round_trip_rtl(N),
                                f"N={N}: RTL round trip broke")


def _load_hex(path):
    """stimulus.txt: hex re/im with two's-complement sign extension."""
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            p = ln.split()
            re_ = int(p[0], 16)
            im_ = int(p[1], 16)
            re_ = re_ - (1 << 16) if re_ & 0x8000 else re_
            im_ = im_ - (1 << 16) if im_ & 0x8000 else im_
            out.append((re_, im_))
    return out


if __name__ == "__main__":
    unittest.main()
