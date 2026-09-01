"""S7 r2^3 -- golden model development.

Part 1 (this file, step 1): the batch contract ``fft_fixed_batch_r23`` --
the merged-triple DIF recursion with ONE product per non-trivial output:

    s_i = round(a_i+a_{i+4}, s0);  d_i = a_i-a_{i+4}          (i = 0..3)
    p0 = round(s0+s2, s1);  p1 = round(s1+s3, s1)
    q0 = s0-s2;             q1 = s1-s3                        (exact)
    r1 = rot45(d1);         r3 = rot45(d3)     (the ONLY two rotates)
    y0 = round(p0+p1, s2)
    y1 = cmul(bm + (r1 - j r3), T[  j*8^m], td+s0+s1+s2)   bm = d0 - j d2
    y2 = cmul(q0 - j q1,        T[2 j*8^m], td+s1+s2)
    y3 = cmul(bp + (r3 - j r1), T[3 j*8^m], td+s0+s1+s2)   bp = d0 + j d2
    y4 = cmul(p0-p1,            T[4 j*8^m], td+s2)
    y5 = cmul(bp - (r1 - j r3), T[5 j*8^m], td+s0+s1+s2)
    y6 = cmul(q0 + j q1,        T[6 j*8^m], td+s1+s2)
    y7 = cmul(bp - (r3 - j r1), T[7 j*8^m], td+s0+s1+s2)
    x[off + j + bitrev3(k)*G] <- y_k          (bitrev3 = 0,4,2,6,1,5,3,7)

Verified here against the plain radix-2 batch (few-LSB delta, identical
SQNR) and numpy, fwd+inv, before any schedule/streaming code exists.
"""
import math
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config import FFTConfig
from quant import (round_shift, complex_multiply_karatsuba, quantize_output)
from twiddles import canonical_twiddles
from golden import fft_fixed_batch, fft_float_reference, _SDFStage

BITREV3 = [0, 4, 2, 6, 1, 5, 3, 7]


def _q8(td):
    return math.floor(math.sqrt(2) / 2 * (1 << td) + 0.5)


def fft_fixed_batch_r23(samples, cfg, twiddles=None):
    """Fixed-point DIF batch reference, radix-2^3 contract (S7)."""
    N = cfg.num_points
    n = cfg.num_stages
    shifts = cfg.shifts
    td = cfg.twiddle_decimal
    if twiddles is None:
        twiddles = canonical_twiddles(N, cfg.twiddle_width, td, cfg.inverse)
    js = -1 if not cfg.inverse else 1      # kernel +-j folds: fwd -j, inv +j
    q8 = _q8(td)

    def jmul(z):                           # js*j*z
        return (-js * z[1], js * z[0])

    def rot(z):                            # rotate by js*-45 degrees
        xr, xi = z
        return (round_shift((xr - js * xi) * q8, td),
                round_shift((xi + js * xr) * q8, td))

    def cmul(u, t, sh):
        pr, pi = complex_multiply_karatsuba(u[0], u[1], t[0], t[1])
        return (round_shift(pr, sh), round_shift(pi, sh))

    x = [[re, im] for re, im in samples]

    m = 0
    while 3 * m + 2 < n:
        s0, s1, s2 = shifts[3 * m], shifts[3 * m + 1], shifts[3 * m + 2]
        G = N >> (3 * m + 3)
        base = 8 ** m
        S3 = td + s0 + s1 + s2
        for b in range(base):
            off = b * (N // base)
            for j in range(G):
                a = [x[off + j + i * G] for i in range(8)]
                s = [(round_shift(a[i][0] + a[i + 4][0], s0),
                      round_shift(a[i][1] + a[i + 4][1], s0)) for i in range(4)]
                d = [(a[i][0] - a[i + 4][0], a[i][1] - a[i + 4][1])
                     for i in range(4)]
                p0 = (round_shift(s[0][0] + s[2][0], s1),
                      round_shift(s[0][1] + s[2][1], s1))
                p1 = (round_shift(s[1][0] + s[3][0], s1),
                      round_shift(s[1][1] + s[3][1], s1))
                q0 = (s[0][0] - s[2][0], s[0][1] - s[2][1])
                q1 = (s[1][0] - s[3][0], s[1][1] - s[3][1])
                r1 = rot(d[1])
                r3 = rot(d[3])
                bm = (d[0][0] + jmul(d[2])[0], d[0][1] + jmul(d[2])[1])
                bp = (d[0][0] - jmul(d[2])[0], d[0][1] - jmul(d[2])[1])
                jm_r3 = jmul(r3)
                jm_r1 = jmul(r1)
                c1 = (bm[0] + r1[0] + jm_r3[0], bm[1] + r1[1] + jm_r3[1])
                c3 = (bp[0] + r3[0] + jm_r1[0], bp[1] + r3[1] + jm_r1[1])
                c5 = (bm[0] - r1[0] - jm_r3[0], bm[1] - r1[1] - jm_r3[1])
                c7 = (bp[0] - r3[0] - jm_r1[0], bp[1] - r3[1] - jm_r1[1])
                cq2 = (q0[0] + jm_q1[0], q0[1] + jm_q1[1]) if False else \
                      (q0[0] + jmul(q1)[0], q0[1] + jmul(q1)[1])
                cq6 = (q0[0] - jmul(q1)[0], q0[1] - jmul(q1)[1])
                y0 = (round_shift(p0[0] + p1[0], s2),
                      round_shift(p0[1] + p1[1], s2))
                y1 = cmul(c1, twiddles[(j * base) % N], S3)
                y2 = cmul(cq2, twiddles[(2 * j * base) % N], td + s1 + s2)
                y3 = cmul(c3, twiddles[(3 * j * base) % N], S3)
                y4 = cmul((p0[0] - p1[0], p0[1] - p1[1]),
                          twiddles[(4 * j * base) % N], td + s2)
                y5 = cmul(c5, twiddles[(5 * j * base) % N], S3)
                y6 = cmul(cq6, twiddles[(6 * j * base) % N], td + s1 + s2)
                y7 = cmul(c7, twiddles[(7 * j * base) % N], S3)
                for k, y in enumerate((y0, y1, y2, y3, y4, y5, y6, y7)):
                    x[off + j + BITREV3[k] * G] = [y[0], y[1]]
        m += 1
    # leftover stages (n mod 3): plain radix-2, same as the r22 contract
    for s in range(3 * m, n):
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
                cr, ci = twiddles[(j << s) % N]
                pr, pi = complex_multiply_karatsuba(dr, di, cr, ci)
                sh = td + sig
                x[i2] = [round_shift(pr, sh), round_shift(pi, sh)]

    return [quantize_output(re, im, cfg.sample_decimal,
                            cfg.output_width, cfg.output_decimal)
            for re, im in x]


def check(N, inverse=False, sample_width=16, tw=18, td=17):
    cfg = FFTConfig(num_points=N, sample_width=sample_width,
                    twiddle_width=tw, twiddle_decimal=td, inverse=inverse)
    import random
    rng = random.Random(1234 + N + 7 * inverse)
    hi = 1 << (sample_width - 1)
    samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
               for _ in range(N)]

    plain = fft_fixed_batch(samples, cfg)
    r23 = fft_fixed_batch_r23(samples, cfg)

    # delta vs plain r2 (both in output quantization)
    dmax = max(abs(a[0] - b[0]) + abs(a[1] - b[1])
               for a, b in zip(plain, r23))
    # SQNR of both vs the float reference, in output LSBs
    def sqnr_of(out):
        den = 0.0
        sig = 0.0
        for (re, im), c in zip(out, ref_scaled):
            v = complex(re, im)
            e = v - c
            sig += abs(v) ** 2
            den += abs(e) ** 2
        return 10 * math.log10(sig / den) if den else float("inf")

    ref = fft_float_reference([complex(re, im) for re, im in samples],
                              inverse)
    ref_scaled = [c * N / (1 << cfg.sample_decimal) for c in ref]
    s_plain = sqnr_of(plain)
    s_r23 = sqnr_of(r23)
    print(f"N={N:5d} inv={int(inverse)}  max|delta| vs plain = {dmax:3d}  "
          f"SQNR plain = {s_plain:6.2f}  r23 = {s_r23:6.2f} dB")
    return dmax, s_plain, s_r23


if __name__ == "__main__":
    worst = 0
    for N in (8, 16, 32, 64, 128, 256, 512, 1024):
        for inv in (False, True):
            dmax, sp, sr = check(N, inv)
            worst = max(worst, dmax)
            assert abs(sp - sr) < 0.05, "SQNR mismatch"
    print(f"\nworst |delta| vs plain r2 = {worst} LSB (rounding-placement "
          f"delta expected, like the r22 re-pin)")


# ----------------------------------------------------------------------
# Part 2: cycle-accurate streaming R2^3 DIF stage.
#
# Schedule (derived in notes.md; period 8G, G = N/8^{m+1}, group g's
# members a_i at block phases i*G+g):
#
#   k in [0,4G):   a0..a3 -> ring0 (4G, addr k)          [write]
#   k in [4G,8G):  a4..a7 arrive; ring0 read-old pairs:
#                  sA_i = round(a_i+a_{i+4}, s0), dA_i = a_i-a_{i+4}
#     [4G,6G):     ringA_s <- sA_0,sA_1 (addr g, G+g); ringA_d0 <- dA_0
#     [5G,6G):     rotate unit A pushes rot(dA_1)        (r1 readable +4)
#     [6G,7G):     sA_2/dA_2: p_0 = round(sA_0+sA_2,s1), q_0 = sA_0-sA_2
#                  -> ringB_p/ringB_q (addr g); ringA_d2 <- dA_2
#     [7G,8G):     sA_3/dA_3: p_1, q_1 (regs); y0 = round(p0+p1,s2);
#                  y4 = cmul(p0-p1, T[4g*8^m], td+s2) -> pfifo[7G+g];
#                  rotate unit B pushes rot(dA_3)        (r3 readable +4)
#   Products (next block; ONE cmul slot/clk, slot [2G,3G) idle):
#     [0,G):   y2 = cmul(q0 - j q1, T[2g*8^m], td+s1+s2) -> pfifo[0+g]
#     [G,2G):  y6 = cmul(q0 + j q1, T[6g*8^m], td+s1+s2) -> pfifo[G+g]
#     [3G,4G): y1 = cmul(bm+(r1-j r3), T[  g*8^m], td+s0+s1+s2) -> [3G+g]
#     [4G,5G): y5 = cmul(bp+(r1+j r3), T[5g*8^m], td+s0+s1+s2) -> [4G+g]
#     [5G,6G): y3 = cmul(bp+(r3-j r1), T[3g*8^m], td+s0+s1+s2) -> [5G+g]
#     [6G,7G): y7 = cmul(bp-(r3-j r1), T[7g*8^m], td+s0+s1+s2) -> [6G+g]
#   with bm = dA_0 - j dA_2 (ringA_d0/ringA_d2 reads), bp = dA_0 + j dA_2.
#
#   Emission (latency 7G+2): position p's value returns at p+7G+2. The
#   output mux at clock pos serves p = pos-(7G+1): t = p mod 8G,
#   member = (t//G + 1) mod 8, g = p mod G; member 0 = y0_reg (direct),
#   else pfifo[BASE[member] + g] with BASE: y4->7G, y2->0, y6->G,
#   y1->3G, y5->4G, y3->5G, y7->6G (static class slots, 8G deep, slot
#   2G idle). Write->read lags: G+1 (y4/y2/y6), 1 (y1/y5/y3/y7) -- all
#   >= 1, so the RTL's uniform +H operand-phase shift (H = pipeline
#   depth, r22-style k1..k8 chain) needs no model change for any H.
#
#   Readiness (G=1 worst case): r3 readable at 7G+g+4 = slot of y1
#   (3G+g of the next block) EXACTLY at G=1; every larger G has slack.
# ----------------------------------------------------------------------

class _R23DIFStage:
    """One radix-2^3 DIF stage (triple (3m, 3m+1, 3m+2) merged).

    Period 8G (G = N/8^{m+1}); group g's members a_i at phases i*G+g.
    Schedule (all ring reads pre-edge snapshot, RTL read-old):

      [0,4G):  a0..a3 -> ring0 (4G); staggered y2/y6/y1 products of the
               PREVIOUS block (slots [0,G), [G,2G), [3G,4G); [2G,3G) idle)
      [4G,5G): a4: sA_0 -> ringA_s[g], dA_0 -> ringA_d0[g]
      [5G,6G): a5: sA_1 -> ringA_s[G+g], dA_1 -> ringA_d1[g]
      [6G,7G): a6: sA_2; p_0 -> ringB_p[g], q_0 -> ringB_q[g];
               bm/bp = dA_0 +- j dA_2 -> ringBB[g]/[G+g]
      [7G,8G): a7: sA_3; p_1/q_1 regs; y0 -> y0_reg;
               y4 -> pfifo[7G+g]; q_1 -> ringB_q1[g];
               rot(dA_3) -> unit-B pipe (ringR[G+g] written at +3)
      unit A:  reads ringA_d1[g] at [10G-3+g], rot pipe, ringR[g]
               written at 10G+g (phase (k-2G) mod 8G < G)
      slots:   y1 [3G,4G)+8G, y5 [4G,5G)+8G, y3 [5G,6G)+8G,
               y7 [6G,7G)+8G -- operands ringBB + ringR
      emission: latency 7G+2; member windows [(i+7)G+1, (i+8)G+1);
               member 0 = y0_reg, else pfifo[BASE[member]+g].

    The rotate hold values and q_1 live in small rings because G > 1
    groups overlap in flight (a register would be clobbered)."""

    def __init__(self, m: int, N: int, sigma0: int, sigma1: int,
                 sigma2: int, td: int, tw: Sequence[Complex],
                 inverse: bool = False):
        self.m = m
        self.N = N
        self.sig = (sigma0, sigma1, sigma2)
        self.td = td
        self.G = N >> (3 * m + 3)
        self.base = 8 ** m
        self.js = -1 if not inverse else 1
        self.q8 = _q8(td)
        self.tw = tw
        G = self.G
        self.ring0 = [(0, 0)] * (4 * G)
        self.ringA_s = [(0, 0)] * (2 * G)
        self.ringA_d0 = [(0, 0)] * G
        self.ringA_d1 = [(0, 0)] * G
        self.ringB_p = [(0, 0)] * G
        self.ringB_q = [(0, 0)] * G
        self.ringB_q1 = [(0, 0)] * G
        self.ringBB = [(0, 0)] * (2 * G)     # bm (g) / bp (G+g)
        self.ringR = [(0, 0)] * (2 * G)      # r1 (g) / r3 (G+g)
        self.pfifo = [(0, 0)] * (8 * G)
        self.rotA = [(0, 0)] * 3             # free-running pipes
        self.rotB = [(0, 0)] * 3
        self.p1_reg = (0, 0)
        self.y0_reg = (0, 0)
        self.out = (0, 0)
        self.BASE = {1: 7 * G, 2: 0, 3: G, 4: 3 * G,
                     5: 4 * G, 6: 5 * G, 7: 6 * G}

    @property
    def latency(self) -> int:
        return 7 * self.G + 2

    def _rot(self, z):
        xr, xi = z
        return (round_shift((xr - self.js * xi) * self.q8, self.td),
                round_shift((xi + self.js * xr) * self.q8, self.td))

    def _prod(self, z, t, sh):
        pr, pi = complex_multiply_karatsuba(z[0], z[1], t[0], t[1])
        return (round_shift(pr, sh), round_shift(pi, sh))

    def _jm(self, z):                      # js*j*z
        return (-self.js * z[1], self.js * z[0])

    def step(self, x: Complex, pos: int) -> Complex:
        G = self.G
        ret = self.out
        r, i = x
        k = pos % (8 * G)
        g = pos % G
        N = self.N
        s0, s1, s2 = self.sig
        td = self.td
        S3 = td + s0 + s1 + s2
        tb = self.base

        # ---------------- snapshot reads (pre-edge) ----------------
        r1 = self.ringR[g]
        r3 = self.ringR[G + g]
        bm = self.ringBB[g]
        bp = self.ringBB[G + g]
        # emission: serves output position pos-(7G+1)
        t = (pos - 1) % (8 * G)
        member = (t // G + 1) % 8
        g_out = t % G
        y0_out = self.y0_reg
        pf_out = (self.pfifo[self.BASE[member] + g_out]
                  if member != 0 else None)
        a0 = self.ring0[k - 4 * G] if k >= 4 * G else None
        sA0 = self.ringA_s[g] if 6 * G <= k < 7 * G else None
        sA1 = self.ringA_s[G + g] if 7 * G <= k < 8 * G else None
        p0_rb = self.ringB_p[g] if 7 * G <= k < 8 * G else None
        q0_rb = self.ringB_q[g] if k < 2 * G else None
        d0_r = self.ringA_d0[g] if 6 * G <= k < 7 * G else None
        # the +3 rotate pipeline hop shifts the phase->group map by 3
        d1_r = self.ringA_d1[(g + 3) % G] \
            if 2 * G <= (k + 3) % (8 * G) < 3 * G else None
        rotA2 = self.rotA[2]
        rotB2 = self.rotB[2]

        # ---------------- compute + writes ----------------
        cur = y0_out if member == 0 else pf_out
        vA = None
        vB = None

        if k < 4 * G:
            self.ring0[k] = (r, i)
            if k < G:
                jr1 = self._jm(self.ringB_q1[g])
                c2 = (q0_rb[0] + jr1[0], q0_rb[1] + jr1[1])
                self.pfifo[g] = self._prod(c2, self.tw[(2 * g * tb) % N],
                                           td + s1 + s2)
            elif k < 2 * G:
                jr1 = self._jm(self.ringB_q1[g])
                c6 = (q0_rb[0] - jr1[0], q0_rb[1] - jr1[1])
                self.pfifo[G + g] = self._prod(c6, self.tw[(6 * g * tb) % N],
                                               td + s1 + s2)
            elif k >= 3 * G:
                jr3 = self._jm(r3)
                c1 = (bm[0] + r1[0] + jr3[0], bm[1] + r1[1] + jr3[1])
                self.pfifo[3 * G + g] = self._prod(c1, self.tw[(g * tb) % N],
                                                   S3)
            # [2G,3G): idle cmul slot
        else:
            sA = (round_shift(a0[0] + r, s0), round_shift(a0[1] + i, s0))
            dA = (a0[0] - r, a0[1] - i)
            if k < 5 * G:
                self.ringA_s[g] = sA
                self.ringA_d0[g] = dA
            elif k < 6 * G:
                self.ringA_s[G + g] = sA
                self.ringA_d1[g] = dA
            elif k < 7 * G:
                p0 = (round_shift(sA0[0] + sA[0], s1),
                      round_shift(sA0[1] + sA[1], s1))
                self.ringB_p[g] = p0
                self.ringB_q[g] = (sA0[0] - sA[0], sA0[1] - sA[1])
                jm2 = self._jm(dA)
                self.ringBB[g] = (d0_r[0] + jm2[0], d0_r[1] + jm2[1])
                self.ringBB[G + g] = (d0_r[0] - jm2[0], d0_r[1] - jm2[1])
            else:
                p1 = (round_shift(sA1[0] + sA[0], s1),
                      round_shift(sA1[1] + sA[1], s1))
                self.p1_reg = p1
                q1 = (sA1[0] - sA[0], sA1[1] - sA[1])
                self.ringB_q1[g] = q1
                self.y0_reg = (round_shift(p0_rb[0] + p1[0], s2),
                               round_shift(p0_rb[1] + p1[1], s2))
                y4 = self._prod((p0_rb[0] - p1[0], p0_rb[1] - p1[1]),
                                self.tw[(4 * g * tb) % N], td + s2)
                self.pfifo[7 * G + g] = y4
                vB = self._rot(dA)         # rotate unit B input (dA_3)

        # rotate unit A input (dA_1 re-read from its ring, 3 clocks
        # before the ringR write)
        if 2 * G <= (k + 3) % (8 * G) < 3 * G:
            vA = self._rot(d1_r)

        # free-running rotate pipes + gated ringR writes
        self.rotA = [vA if vA is not None else (0, 0),
                     self.rotA[0], self.rotA[1]]
        self.rotB = [vB if vB is not None else (0, 0),
                     self.rotB[0], self.rotB[1]]
        if (k - 2 * G) % (8 * G) < G:
            self.ringR[g] = rotA2
        if (k - 3) % (8 * G) >= 7 * G:
            self.ringR[G + (g - 3) % G] = rotB2

        if 4 * G <= k < 5 * G:
            # y5: bm - (r1 + j r3)
            jr3 = self._jm(r3)
            c5 = (bm[0] - r1[0] - jr3[0], bm[1] - r1[1] - jr3[1])
            self.pfifo[4 * G + g] = self._prod(c5, self.tw[(5 * g * tb) % N],
                                               S3)
        elif 5 * G <= k < 6 * G:
            # y3: bp + (r3 + j r1)
            jr1 = self._jm(r1)
            c3 = (bp[0] + r3[0] + jr1[0], bp[1] + r3[1] + jr1[1])
            self.pfifo[5 * G + g] = self._prod(c3, self.tw[(3 * g * tb) % N],
                                               S3)
        elif 6 * G <= k < 7 * G:
            # y7: bp - (r3 + j r1)
            jr1 = self._jm(r1)
            c7 = (bp[0] - r3[0] - jr1[0], bp[1] - r3[1] - jr1[1])
            self.pfifo[6 * G + g] = self._prod(c7, self.tw[(7 * g * tb) % N],
                                               S3)

        self.out = cur
        return ret


# ----------------------------------------------------------------------
# Part 3: the streaming chain (mirrors R22SDFGoldenModel)
# ----------------------------------------------------------------------

class R23SDFGoldenModel:
    """Cycle-accurate streaming model of the R2^3 DIF pipeline (S7).

    One :class:`_R23DIFStage` per merged triple; the leftover stages
    (n mod 3) are plain radix-2 :class:`_SDFStage` instances with
    phase preloads of (-(upstream latency)) mod 2D (the r22 leftover
    parity trick, generalized). Stage latency 7G+2; the RTL adds a
    uniform operand-phase shift H (any H works -- the schedule's
    write->read lags are >= 1 everywhere)."""

    def __init__(self, cfg: FFTConfig):
        if cfg.ssr != 1:
            raise NotImplementedError("R23 model supports ssr=1 only")
        if cfg.is_dit or cfg.input_order != "native" \
                or cfg.output_order != "bitreversed":
            raise NotImplementedError(
                "R23 model covers native->bitreversed only")
        self.cfg = cfg
        N = cfg.num_points
        self.N = N
        n = cfg.num_stages
        td = cfg.twiddle_decimal
        tw = canonical_twiddles(N, cfg.twiddle_width, td, cfg.inverse)
        self.stages = []
        m = 0
        while 3 * m + 3 <= n:
            self.stages.append(_R23DIFStage(
                m, N, cfg.shifts[3 * m], cfg.shifts[3 * m + 1],
                cfg.shifts[3 * m + 2], td, tw, cfg.inverse))
            m += 1
        # leftover plain radix-2 stages (n mod 3 of them), phase-preloaded
        self.leftovers = []
        lat = sum(st.latency for st in self.stages)
        for s in range(3 * m, n):
            D = N >> (s + 1)
            tw_slice = [tw[(j << s) % N] for j in range(D)]
            st = _SDFStage(s, N, cfg.shifts[s], td, tw_slice, dit=False)
            for _ in range((-lat) % (2 * D)):
                st.step(0, 0)
            self.leftovers.append(st)
            lat += D + _SDFStage.NLAYERS
        self.latency = lat

    def reset(self):
        import copy
        self.stages = [copy.deepcopy(st.__class__(
            st.m, self.N, st.sig[0], st.sig[1], st.sig[2], st.td, st.tw,
            st.js == 1)) for st in self.stages]
        # leftovers: rebuild with the same preloads
        lat = sum(st.latency for st in self.stages)
        n = self.cfg.num_stages
        td = self.cfg.twiddle_decimal
        tw = canonical_twiddles(self.N, self.cfg.twiddle_width, td,
                                self.cfg.inverse)
        news = []
        for idx, st in enumerate(self.leftovers):
            s = 3 * (n // 3) + idx
            D = self.N >> (s + 1)
            tw_slice = [tw[(j << s) % N] for j in range(D)]
            ns = _SDFStage(s, self.N, self.cfg.shifts[s], td, tw_slice,
                           dit=False)
            for _ in range((-lat) % (2 * D)):
                ns.step(0, 0)
            news.append(ns)
            lat += D + _SDFStage.NLAYERS
        self.leftovers = news

    def process_stream(self, samples, markers=None, frames=None):
        N = self.N
        T = len(samples)
        raw = []
        for pos in range(T + self.latency):
            src = samples[pos] if pos < T else (0, 0)
            cur = (src[0], src[1])
            up = 0
            for st in self.stages:
                cur = st.step(cur, pos - up)
                up += st.latency
            for st in self.leftovers:
                cur = st.step(cur[0], cur[1])
            raw.append(cur)
        raw = raw[self.latency:]
        assert len(raw) == T
        outs = []
        for f in range(len(raw) // N):
            frame = raw[f * N:(f + 1) * N]
            q = [quantize_output(re, im, self.cfg.sample_decimal,
                                 self.cfg.output_width,
                                 self.cfg.output_decimal)
                 for re, im in frame]
            if markers is not None:
                for k, (re, im) in enumerate(q):
                    idx = f * N + k
                    outs.append((re, im) + tuple(markers[idx]))
            else:
                outs.extend(q)
        return outs

    def process_stream_simple(self, samples):
        return self.process_stream(samples)


# ----------------------------------------------------------------------
# verification
# ----------------------------------------------------------------------

def verify_stream(N, inverse=False, sample_width=16, tw=18, td=17,
                  frames=2, seed=99):
    cfg = FFTConfig(num_points=N, sample_width=sample_width,
                    twiddle_width=tw, twiddle_decimal=td, inverse=inverse)
    import random
    rng = random.Random(seed + N + 13 * inverse)
    hi = 1 << (sample_width - 1)
    T = frames * N
    samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
               for _ in range(T)]
    markers = [(1 if kk % N == 0 else 0, 1 if kk % N == N - 1 else 0)
               for kk in range(T)]

    m = R23SDFGoldenModel(cfg)
    got = m.process_stream(samples, markers=markers)
    # the batch contract processes ONE frame per call -- compare per frame
    batch = []
    for f in range(frames):
        batch.extend(fft_fixed_batch_r23(samples[f*N:(f+1)*N], cfg))
    bad = None
    for idx, ((gre, gim, u, l), (bre, bim)) in enumerate(zip(got, batch)):
        if (gre, gim) != (bre, bim):
            bad = (idx, (gre, gim, u, l), (bre, bim))
            break
    n_mk = sum(1 for o in got if o[2] == 1)
    status = "OK" if bad is None else f"FAIL at {bad}"
    print(f"stream N={N:5d} inv={int(inverse)} w={sample_width:2d} "
          f"lat={m.latency:5d} markers={n_mk}/{frames}  {status}")
    return bad is None


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "stream":
    ok = True
    for N in (8, 16, 32, 64, 128, 256, 512, 1024):
        for inv in (False, True):
            ok &= verify_stream(N, inv)
    for w in (12, 20):
        ok &= verify_stream(64, False, sample_width=w)
        ok &= verify_stream(256, True, sample_width=w)
    print("\nALL BIT-EXACT" if ok else "\nFAILURES")
