"""Spike S5a: radix-2^2 folding -- numerical contract check (P7).

Question: is the classic R2^2 (radix-4 DIF) butterfly bit-identical to
the pinned radix-2 golden (fft_fixed_batch), or does it change the
quantization contract?

Key facts established by derivation:
  1. The rotation identity T[i + N/4] == -j * T[i] holds BIT-EXACTLY in
     the canonical table (magnitude-first construction: A/B tables are
     |cos|/|sin| of the SAME magnitude, so the quarter-wave mirror is an
     exact sign flip). Verified exhaustively here.
  2. The plain two-stage DIF per 4-group computes FOUR products with
     FOUR separate roundings:
        c0 = round(cmul(a0-a2, T[i]),        td+s0)
        c1 = round(cmul(a1-a3, T[i+N/4]),    td+s0)
        y2 = round(cmul((a0+a2)-(a1+a3), T[2i]), td+s1)
        y3 = round(cmul(c0-c1, T[2i]),       td+s1)
  3. The classic R2^2/radix-4 DIF computes THREE products (the diff
     combine happens BEFORE the multiply, via the exact -j rotation):
        y1 = round(cmul((a0-a2) - j(a1-a3), T[i]),  td+s0+s1)
        y2 = round(cmul((a0+a2)-(a1+a3), T[2i]),    td+s1)
        y3 = round(cmul((a0-a2) + j(a1-a3), T[3i]), td+s0+s1)
     Since round() is NOT rotation-invariant (round-half-up bias), the
     two formulations differ at rounding boundaries -- the "same shift
     points => bit-identical" claim (PLAN appendix A) is expected to be
     FALSE for the fused Q-format datapath.

This spike measures the delta and the SQNR impact to decide whether to
re-pin the golden to the R2^2 contract (P7 proceeds) or keep the radix-2
contract (P7 dies).
"""
import math
import random
import sys

sys.path.insert(0, "src")

from config import FFTConfig
from quant import complex_multiply_karatsuba, round_shift
from twiddles import canonical_twiddles


# ----------------------------------------------------------------------
# 1. rotation identity check
# ----------------------------------------------------------------------

def check_rotation_identity():
    bad = 0
    for N in (8, 16, 32, 64, 128, 256, 1024):
        for width, dec in ((18, 17), (12, 11), (8, 7), (18, 16), (10, 6)):
            for inv in (False, True):
                tw = canonical_twiddles(N, width, dec, inverse=inv)
                j_sign = -1 if not inv else 1    # W^{N/4} = -j fwd, +j inv
                for i in range(N):
                    # j_sign*j * T[i]: fwd (im,-re), inv (-im,re)
                    rotated = (-j_sign * tw[i][1], j_sign * tw[i][0])
                    if tw[(i + N // 4) % N] != rotated:
                        bad += 1
    return bad


# ----------------------------------------------------------------------
# 2. plain two-stage pair (batch, stages 0..1 of a DIF of size N)
# ----------------------------------------------------------------------

def plain_pair(x, N, tw, shifts, td):
    """The pinned radix-2 DIF stages 0+1 for one 4-group's worth of the
    full array x (all 4-group blocks); returns the updated array."""
    s0, s1 = shifts[0], shifts[1]
    for i in range(N // 4):
        a0 = x[i]
        a2 = x[i + N // 2]
        a1 = x[i + N // 4]
        a3 = x[i + 3 * N // 4]
        # stage 0: pairs (i, i+N/2) tw T[i]; (i+N/4, i+3N/4) tw T[i+N/4]
        b0 = _sum(a0, a2, s0)
        c0 = _prod((a0[0]-a2[0], a0[1]-a2[1]), tw[i], td + s0)
        b1 = _sum(a1, a3, s0)
        c1 = _prod((a1[0]-a3[0], a1[1]-a3[1]), tw[(i + N // 4) % N], td + s0)
        # stage 1: pairs (b0,b1) tw T[2i]; (c0,c1) tw T[2i]
        x[i] = _sum(b0, b1, s1)
        x[i + N // 4] = _prod((b0[0]-b1[0], b0[1]-b1[1]), tw[(2 * i) % N], td + s1)
        x[i + N // 2] = _sum(c0, c1, s1)
        x[i + 3 * N // 4] = _prod((c0[0]-c1[0], c0[1]-c1[1]), tw[(2 * i) % N], td + s1)
    return x


def _sum(p, q, sh):
    return (round_shift(p[0] + q[0], sh), round_shift(p[1] + q[1], sh))


def _prod(p, tw, sh):
    r, i = complex_multiply_karatsuba(p[0], p[1], tw[0], tw[1])
    return (round_shift(r, sh), round_shift(i, sh))


# ----------------------------------------------------------------------
# 3. R2^2 (radix-4 DIF) pair -- candidate re-pinned contract
# ----------------------------------------------------------------------

def r22_pair(x, N, tw, shifts, td, inverse=False):
    """Classic radix-2^2 DIF pair: exact +/-j diff combine BEFORE the
    multiply; three products per 4-group rounded once each at the fused
    shift. Sum paths keep the two sub-stage roundings."""
    s0, s1 = shifts[0], shifts[1]
    js = -1 if not inverse else 1      # W^{N/4} = -j fwd, +j inv
    for i in range(N // 4):
        a0 = x[i]
        a2 = x[i + N // 2]
        a1 = x[i + N // 4]
        a3 = x[i + 3 * N // 4]
        # sums: same two roundings as plain (s0 then s1)
        b0 = _sum(a0, a2, s0)
        b1 = _sum(a1, a3, s0)
        x[i] = _sum(b0, b1, s1)
        x[i + N // 4] = _prod((b0[0]-b1[0], b0[1]-b1[1]), tw[(2 * i) % N], td + s1)
        # diffs: exact +/-j combine, ONE product at the fused shift
        dr = (a0[0] - a2[0]) - js * (a1[1] - a3[1])   # d0 - js*j*d1
        di = (a0[1] - a2[1]) + js * (a1[0] - a3[0])
        x[i + N // 2] = _prod((dr, di), tw[i], td + s0 + s1)
        er = (a0[0] - a2[0]) + js * (a1[1] - a3[1])   # d0 + js*j*d1
        ei = (a0[1] - a2[1]) - js * (a1[0] - a3[0])
        x[i + 3 * N // 4] = _prod((er, ei), tw[(3 * i) % N], td + s0 + s1)
    return x


# ----------------------------------------------------------------------
# 4. full-transform comparison + SQNR
# ----------------------------------------------------------------------

def run_pair(cfg, rng):
    N = cfg.num_points
    n = cfg.num_stages
    assert n >= 2
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    td = cfg.twiddle_decimal
    shifts = cfg.shifts
    # first pair only (stages 0,1) on random data, then continue with the
    # remaining stages per the pinned golden so the comparison is on the
    # full transform.
    samples = [(rng.randint(-2 ** (cfg.sample_width - 1),
                            2 ** (cfg.sample_width - 1) - 1),
                rng.randint(-2 ** (cfg.sample_width - 1),
                            2 ** (cfg.sample_width - 1) - 1))
               for _ in range(N)]

    # plain: pinned golden (single source of truth)
    from golden import fft_fixed_batch
    plain = fft_fixed_batch(samples, cfg)

    # r22: swap the first pair's butterfly, keep the rest identical
    x = [[r, i] for r, i in samples]
    r22_pair(x, N, tw, shifts, td, inverse=cfg.inverse)
    for s in range(2, n):
        D = N >> (s + 1)
        sig = shifts[s]
        for start in range(0, N, 2 * D):
            for j in range(D):
                i1 = start + j
                i2 = i1 + D
                ar, ai = x[i1]
                br, bi = x[i2]
                x[i1] = [round_shift(ar + br, sig),
                         round_shift(ai + bi, sig)]
                dr, di = ar - br, ai - bi
                cr, ci = tw[(j << s) % N]
                pr, pi = complex_multiply_karatsuba(dr, di, cr, ci)
                sh = td + sig
                x[i2] = [round_shift(pr, sh), round_shift(pi, sh)]
    from quant import quantize_output
    r22 = [quantize_output(re, im, cfg.sample_decimal,
                           cfg.output_width, cfg.output_decimal)
           for re, im in x]

    max_d = max(abs(p[0] - r[0]) for p, r in zip(plain, r22)) \
        if plain else 0
    max_d = max(max_d, max(abs(p[1] - r[1]) for p, r in zip(plain, r22)))

    # SQNR of both vs float
    from golden import fft_float_radix2
    fref = fft_float_radix2([complex(r, i) for r, i in samples])
    scale = 1 << cfg.sample_decimal

    def sqnr(vals):
        err_p = sum(abs(v[0] / scale - fref[k].real) ** 2
                    + abs(v[1] / scale - fref[k].imag) ** 2
                    for k, v in enumerate(vals))
        sig_p = sum(abs(fref[k]) ** 2 for k in range(N))
        return 10 * math.log10(sig_p / err_p) if err_p else float("inf")

    return max_d, sqnr(plain), sqnr(r22)


if __name__ == "__main__":
    print("== rotation identity (T[i+N/4] == -j*T[i]) ==")
    bad = check_rotation_identity()
    print("mismatches:", bad, "=>", "HOLDS bit-exactly" if bad == 0
          else "VIOLATED")

    print("\n== plain radix-2 vs R2^2 pair, bit-exactness + SQNR ==")
    rng = random.Random(1)
    for N in (8, 16, 32, 64):
        for inv in (False, True):
            cfg = FFTConfig(num_points=N, inverse=inv)
            md, sp, sr = run_pair(cfg, rng)
            print(f"N={N:4d} inv={int(inv)}: max|delta|={md:4d}  "
                  f"SQNR plain={sp:6.2f} dB  SQNR r22={sr:6.2f} dB  "
                  f"bit-identical={'YES' if md == 0 else 'NO'}")


# ----------------------------------------------------------------------
# 5. full R2^2 batch reference (candidate new golden) + larger-N SQNR
# ----------------------------------------------------------------------

def fft_fixed_batch_r22(samples, cfg):
    """Full radix-2^2 DIF batch reference (re-pinned contract).

    Stage pairs (2m, 2m+1) merge into one R2^2 group with 3 products per
    4 samples (y1/y3 combine the diffs EXACTLY via the +/-j rotation --
    exact in the canonical table -- then one fused-shift product). Odd
    stage counts leave the last stage as plain radix-2.
    """
    N = cfg.num_points
    n = cfg.num_stages
    shifts = cfg.shifts
    td = cfg.twiddle_decimal
    tw = canonical_twiddles(N, cfg.twiddle_width, td, cfg.inverse)
    js = -1 if not cfg.inverse else 1

    x = [[re, im] for re, im in samples]

    m = 0
    while 2 * m + 1 < n:
        s0, s1 = shifts[2 * m], shifts[2 * m + 1]
        D = N >> (2 * m + 2)          # group depth N/4^{m+1}
        base = 4 ** m                 # twiddle stride W^{j * 4^m}
        for b in range(base):         # 4^m blocks of size N/4^m
            off = b * (N // base)
            for j in range(D):
                a0 = x[off + j]
                a1 = x[off + j + D]
                a2 = x[off + j + 2 * D]
                a3 = x[off + j + 3 * D]
                # sub-stage 2m: sums (two roundings as plain), diffs exact
                b0 = _sum(a0, a2, s0)
                b1 = _sum(a1, a3, s0)
                d0 = (a0[0] - a2[0], a0[1] - a2[1])
                d1 = (a1[0] - a3[0], a1[1] - a3[1])
                # sub-stage 2m+1: three products, one rounding each
                x[off + j] = _sum(b0, b1, s1)
                x[off + j + D] = _prod((b0[0] - b1[0], b0[1] - b1[1]),
                                       tw[(2 * j * base) % N], td + s1)
                x[off + j + 2 * D] = _prod(
                    (d0[0] - js * d1[1], d0[1] + js * d1[0]),
                    tw[(j * base) % N], td + s0 + s1)
                x[off + j + 3 * D] = _prod(
                    (d0[0] + js * d1[1], d0[1] - js * d1[0]),
                    tw[(3 * j * base) % N], td + s0 + s1)
        m += 1
    # leftover last stage (odd n): plain radix-2
    for s in range(2 * m, n):
        D = N >> (s + 1)
        sig = shifts[s]
        for start in range(0, N, 2 * D):
            for j in range(D):
                i1 = start + j
                i2 = i1 + D
                ar, ai = x[i1]
                br, bi = x[i2]
                x[i1] = [round_shift(ar + br, sig),
                         round_shift(ai + bi, sig)]
                dr, di = ar - br, ai - bi
                cr, ci = tw[(j << s) % N]
                pr, pi = complex_multiply_karatsuba(dr, di, cr, ci)
                sh = td + sig
                x[i2] = [round_shift(pr, sh), round_shift(pi, sh)]

    from quant import quantize_output
    return [quantize_output(re, im, cfg.sample_decimal,
                            cfg.output_width, cfg.output_decimal)
            for re, im in x]


def sqnr_of(vals, fref, N, scale):
    err_p = sum(abs(v[0] / scale - fref[k].real) ** 2
                + abs(v[1] / scale - fref[k].imag) ** 2
                for k, v in enumerate(vals))
    sig_p = sum(abs(fref[k]) ** 2 for k in range(N))
    return 10 * math.log10(sig_p / err_p) if err_p else float("inf")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", default="128,256,1024")
    args = p.parse_args()
    print("\n== full R2^2 batch reference: SQNR vs float (larger N) ==")
    from golden import fft_float_radix2, fft_fixed_batch
    rng = random.Random(3)
    for N in (int(s) for s in args.sizes.split(",")):
        for inv in (False, True):
            cfg = FFTConfig(num_points=N, inverse=inv)
            samples = [(rng.randint(-2 ** (cfg.sample_width - 1),
                                    2 ** (cfg.sample_width - 1) - 1),
                        rng.randint(-2 ** (cfg.sample_width - 1),
                                    2 ** (cfg.sample_width - 1) - 1))
                       for _ in range(N)]
            fref = fft_float_radix2([complex(r, i) for r, i in samples])
            scale = 1 << cfg.sample_decimal
            plain = fft_fixed_batch(samples, cfg)
            r22 = fft_fixed_batch_r22(samples, cfg)
            md = max(max(abs(p[k] - r[k]) for k in range(2))
                     for p, r in zip(plain, r22))
            print(f"N={N:4d} inv={int(inv)}: "
                  f"plain-vs-r22 max|delta|={md:4d}  "
                  f"SQNR plain={sqnr_of(plain, fref, N, scale):7.2f} dB  "
                  f"SQNR r22  ={sqnr_of(r22, fref, N, scale):7.2f} dB")
