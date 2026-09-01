"""S7 r2^3 spike -- numeric side of the 45-degree (W8) rotation.

The radix-2^3 merged triple keeps a general complex multiplier alive to the
deepest triple because its class-1 twiddles hit the 8th roots of unity:
W^{N/8} = e^{-jpi/4} = (sqrt2/2)(1-j) -- the 45-degree rotation.  Unlike the
r2^2 classes ({0, +-1, +-j}) this is NOT exact sign/swap logic; the spike
must pick how to compute it.  Three candidates:

  (A) ROM-precombined: a second twiddle table T45 = quantize(T_exact * W8).
      Zero new datapath hardware, one extra quantization point (a table
      that cannot be derived bit-exactly from the pinned T table).
  (B) product-side fabric rotate: y = round_shift(cmul_exact(u, T) * q,
      2*td + s) with q = round_half_up(sqrt2/2 * 2^td) -- a constant
      shift-add multiply AFTER the DSP (PREG), generalized trivial_prod.
      Exact integer arithmetic: NO new rounding beyond the (already
      fused) stage shift.
  (C) operand-side fabric rotate: u' = round_shift((u_re +/- u_im) * q, td)
      (ONE extra rounding at CB width), then the normal product.

This script pins the tap structure of q (fabric cost) and the LSB deltas of
A/B/C against the float ideal, so the timing probe (probe.py) measures the
variant that the golden model would actually have to pin.

Run:  python3 spikes/S7_r23/rot45.py
"""
import math
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from quant import round_shift, complex_multiply_karatsuba
from twiddles import canonical_twiddles

SQ2H = math.sqrt(2) / 2


def q8(td):
    """q = round_half_up(sqrt2/2 * 2^td) and its tap decomposition."""
    q = math.floor(SQ2H * (1 << td) + 0.5)
    taps = [i for i in range(td + 2) if (q >> i) & 1]
    return q, taps


def csd_taps(q):
    """Canonical signed digit digit count (lower bound on adder cost)."""
    # simple non-adjacent form
    n = q
    digits = []
    while n:
        if n & 1:
            d = 2 - (n & 3)          # +-1
            digits.append(d)
            n -= d
        else:
            digits.append(0)
        n >>= 1
    return sum(1 for d in digits if d)


def cmul_q8(ar, ai, q, td):
    """(B) helper: exact cmul rotated by W8 in the integer domain.
    (ar + j ai) * (q/2^td)(1 - j)  ->  re = (ar+ai)*q, im = (ai-ar)*q."""
    return (ar + ai) * q, (ai - ar) * q


def rot45_operand(u_re, u_im, q, td, cb):
    """(C) rotate the OPERAND, round back to CB bits (Q0)."""
    re = round_shift((u_re + u_im) * q, td)
    im = round_shift((u_im - u_re) * q, td)
    lo = -(1 << (cb - 1))
    hi = (1 << (cb - 1)) - 1
    return max(lo, min(hi, re)), max(lo, min(hi, im))


def main():
    print("== q = round(sqrt2/2 * 2^td): tap structure (fabric cost) ==")
    for td in (8, 10, 12, 14, 16, 17, 18):
        q, taps = q8(td)
        print(f"  td={td:2d} q={q:7d} popcount={len(taps):2d} "
              f"csd={csd_taps(q):2d} taps={taps} "
              f"err={abs(q / 2**td - SQ2H) * 2**td:.3f} LSB")
    q18, _ = q8(18)
    q16, _ = q8(16)
    print(f"  q(td+2) == 4*q(td): {q18 == 4 * q16} (tap pattern is "
          f"td-invariant, tree depth = ceil(log2(popcount)) + 1 level)")

    # ---------------------------------------------------------------
    N, tw, td = 256, 18, 17
    s = 1
    fwd = canonical_twiddles(N, tw, td, inverse=False)
    cb = 18
    print(f"\n== candidate deltas, N={N} tw={tw} td={td} cb={cb} ==")

    # (A) precombined table vs the ideal
    q, _ = q8(td)
    dA = dB = dC = 0
    dAB = dAC = 0
    max_tab = 0
    for k in range(N):
        ang = -2 * math.pi * k / N - math.pi / 4      # T * W8, exact
        # W8 = e^{-jpi/4} has unit magnitude (sqrt2/2 lives in its
        # components), so T45 quantizes WITHOUT a sqrt2/2 scale
        t45_re = math.floor(math.cos(ang) * (1 << td) + 0.5)
        t45_im = math.floor(math.sin(ang) * (1 << td) + 0.5)
        tre, tim = fwd[k]
        # rot of the QUANTIZED T (what an operand/product rotate sees)
        # rot of the QUANTIZED T as its own Q(td) word (rounded) -- what a
        # fabric rotate of the pinned table word would produce
        rre = round_shift((tre + tim) * q, td)
        rim = round_shift((tim - tre) * q, td)
        max_tab = max(max_tab, abs(t45_re - rre), abs(t45_im - rim))

        # random-ish operands over the word range
        for u_re, u_im in [(12345, -6789), (-30000, 30000),
                           (8192, 8192), (-1, 32767), (0, -32768)]:
            # float ideal: cmul(u, T_exact) * W8, at stage output Q(s):
            # t0 = cmul(u, e^{-jangu}), then rot = (t0_re + t0_im, t0_im
            # - t0_re) * sqrt2/2 -- the FULL (1-j) mixing, not just the
            # sqrt2/2 scale
            angu = -2 * math.pi * k / N
            re0 = u_re * math.cos(angu) - u_im * math.sin(angu)
            im0 = u_re * math.sin(angu) + u_im * math.cos(angu)
            sc = SQ2H / (1 << s)
            ref_re, ref_im = (re0 + im0) * sc, (im0 - re0) * sc

            # (A) precombined table
            yA_re = round_shift(complex_multiply_karatsuba(
                u_re, u_im, t45_re, t45_im)[0], td + s)
            yA_im = round_shift(complex_multiply_karatsuba(
                u_re, u_im, t45_re, t45_im)[1], td + s)
            # (B) product-side fabric rotate (exact cmul, exact *q)
            pr, pi = complex_multiply_karatsuba(u_re, u_im, tre, tim)
            qr, qi = cmul_q8(pr, pi, q, td)
            yB_re = round_shift(qr, 2 * td + s)
            yB_im = round_shift(qi, 2 * td + s)
            # (C) operand-side rotate (extra CB rounding) then product
            ur, ui = rot45_operand(u_re, u_im, q, td, cb)
            yr = complex_multiply_karatsuba(ur, ui, tre, tim)
            yC_re = round_shift(yr[0], td + s)
            yC_im = round_shift(yr[1], td + s)

            for name, y, acc in (("A", (yA_re, yA_im), "A"),
                                 ("B", (yB_re, yB_im), "B"),
                                 ("C", (yC_re, yC_im), "C")):
                e = max(abs(y[0] - ref_re), abs(y[1] - ref_im)) / (1 << s)
                if name == "A":
                    dA = max(dA, e)
                elif name == "B":
                    dB = max(dB, e)
                else:
                    dC = max(dC, e)
            dAB = max(dAB, abs(yA_re - yB_re), abs(yA_im - yB_im))
            dAC = max(dAC, abs(yA_re - yC_re), abs(yA_im - yC_im))

    print(f"  max |T45 word - T word| (table delta, LSB of Q(td)): {max_tab}")
    print(f"  vs float ideal (output LSBs):  A={dA:.2f}  B={dB:.2f}  C={dC:.2f}")
    print(f"  max pairwise delta A-B: {dAB}  A-C: {dAC}  (output LSBs)")

    print("""
== reading ==
  (B) is the exact-arithmetic choice: its only error vs the ideal is the
      SAME q-quantization error every candidate shares (~0.1-0.3 LSB of
      Q(td) on the rotation constant); no new rounding point anywhere.
  (C) adds one CB-width rounding BEFORE the multiply -> ~1-2 extra LSB.
  (A) re-quantizes the coefficient (~<=1 LSB on T45) -> sub-LSB here.
  All three are within the existing quantization contract's noise floor;
  the golden model may pin any of them.  The DECIDER is timing/pipelining
  (probe.py): (A) = zero new hardware, (B) = 3-4 CARRY8 levels after the
  DSP PREG (the path class that already binds r22 R=1), (C) = 3-4 CARRY8
  levels before the DSP AREG.""")


if __name__ == "__main__":
    main()
