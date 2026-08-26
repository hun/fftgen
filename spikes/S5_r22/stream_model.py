"""Spike S5b: cycle-accurate streaming R2^2 DIF stage model (P7).

The R2^2 stage merges sub-stages 2m (delay 2D) and 2m+1 (delay D),
D = N/4^{m+1}, sharing ONE complex multiplier. Per 4D-clock block the
stream carries D groups of 4 samples (a0..a3 at positions g, g+D, g+2D,
g+3D). All four of group g's values complete at clock 3D+g; the outputs
are staged so each position p's value emerges at clock p + 3D (stage
latency contribution 3D):

  k in [0, D):    out = q2 read   (y2 of the previous block)
  k in [D, 2D):   out = q1 read   (y1)
  k in [2D, 3D):  out = q3 read   (y3)
  k in [3D, 4D):  out = y0 (compute: a3 arrives -> s1; s0,d0 read from
                  the lag-D lines; the sub-stage-2m+1 combine + the
                  exact +/-j diff products y1, y3 are computed and
                  staged for D/2D/3D)

Verified bit-exact against golden.fft_fixed_batch_r22 (the contract).
"""
import sys

sys.path.insert(0, "src")

from golden import fft_fixed_batch_r22
from quant import complex_multiply_karatsuba, round_shift, quantize_output
from twiddles import canonical_twiddles


class R22DIFStage:
    def __init__(self, m, N, sigma0, sigma1, td, tw, inverse=False):
        self.m = m
        self.N = N
        self.sigma0, self.sigma1 = sigma0, sigma1
        self.td = td
        self.D = N >> (2 * m + 2)          # group depth N/4^{m+1}
        self.base = 4 ** m
        self.js = -1 if not inverse else 1
        self.tw = tw
        self.ram = [(0, 0)] * (2 * self.D)      # raw a0/a1 (lag 2D)
        self.sram = [(0, 0)] * self.D           # s0 (lag D)
        self.dram = [(0, 0)] * self.D           # d0 (lag D)
        self.q2 = [(0, 0)] * self.D             # y2 staging (lag D)
        self.q1 = [(0, 0)] * (2 * self.D)       # y1 staging (lag 2D)
        self.q3 = [(0, 0)] * (3 * self.D)       # y3 staging (lag 3D)
        self.out = (0, 0)
        self.rp = 0
        self.sp = 0
        self.q2p = 0
        self.q1p = 0
        self.q3p = 0

    @property
    def latency(self):
        return 3 * self.D

    def step(self, x, pos):
        D = self.D
        out = self.out
        r, i = x
        k = pos % (4 * D)
        g = pos % D

        if k >= 3 * D:
            # a3 arrives: butterfly (a1, a3) -> s1, d1; then combine
            a1 = self.ram[self.rp]
            s1 = (round_shift(a1[0] + r, self.sigma0),
                  round_shift(a1[1] + i, self.sigma0))
            d1 = (a1[0] - r, a1[1] - i)
            s0 = self.sram[self.sp]
            d0 = self.dram[self.sp]
            y0 = (round_shift(s0[0] + s1[0], self.sigma1),
                  round_shift(s0[1] + s1[1], self.sigma1))
            t2 = self.tw[(2 * g * self.base) % self.N]
            y2 = self._prod((s0[0] - s1[0], s0[1] - s1[1]), t2,
                            self.td + self.sigma1)
            t1 = self.tw[(g * self.base) % self.N]
            t3 = self.tw[(3 * g * self.base) % self.N]
            cm = (d0[0] - self.js * d1[1], d0[1] + self.js * d1[0])
            cp = (d0[0] + self.js * d1[1], d0[1] - self.js * d1[0])
            S = self.td + self.sigma0 + self.sigma1
            y1 = self._prod(cm, t1, S)
            y3 = self._prod(cp, t3, S)
            self.q2[self.q2p] = y2          # read D clocks later
            self.q1[self.q1p] = y1          # read 2D later
            self.q3[self.q3p] = y3          # read 3D later
            out = y0
        else:
            if k < D:
                out = self.q2[self.q2p]
            elif k < 2 * D:
                out = self.q1[self.q1p]
            else:
                out = self.q3[self.q3p]
            if k >= 2 * D:
                # a2 arrives: butterfly (a0, a2) -> s0, d0 (lag-D store)
                a0 = self.ram[self.rp]
                self.sram[self.sp] = (round_shift(a0[0] + r, self.sigma0),
                                      round_shift(a0[1] + i, self.sigma0))
                self.dram[self.sp] = (a0[0] - r, a0[1] - i)
            else:
                # a0 / a1: raw store for the lag-2D pairing
                self.ram[self.rp] = (r, i)

        self.rp = (self.rp + 1) % (2 * D)
        self.sp = (self.sp + 1) % D
        self.q2p = (self.q2p + 1) % D
        self.q1p = (self.q1p + 1) % (2 * D)
        self.q3p = (self.q3p + 1) % (3 * D)
        self.out = out
        return out

    def _prod(self, z, t, sh):
        pr, pi = complex_multiply_karatsuba(z[0], z[1], t[0], t[1])
        return (round_shift(pr, sh), round_shift(pi, sh))


def run_stream(cfg):
    """Chain the R2^2 stages in lockstep; leftover stage handled as plain
    SDF via the batch (contract); compare the stream vs the batch."""
    import random
    N = cfg.num_points
    n = cfg.num_stages
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    td = cfg.twiddle_decimal
    stages = []
    m = 0
    while 2 * m + 1 < n:
        stages.append(R22DIFStage(m, N, cfg.shifts[2 * m],
                                  cfg.shifts[2 * m + 1], td, tw,
                                  cfg.inverse))
        m += 1
    nleft = n - 2 * m          # leftover plain stages (odd n)
    total_lat = sum(st.latency for st in stages)

    rng = random.Random(13)
    hi = 2 ** (cfg.sample_width - 1)
    samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
               for _ in range(N)]

    # lockstep chain: each stage sees its upstream output each clock
    cur = samples
    for st in stages:
        nxt = []
        for pos in range(N + st.latency):
            src = cur[pos] if pos < N else (0, 0)
            nxt.append(st.step(src, pos))
        cur = nxt[st.latency:]           # align: position p at clock p+3D
        assert len(cur) == N

    # leftover plain stages (batch-style, mirroring the plain SDF on the
    # stream order) -- same as golden.fft_fixed_batch's remaining stages
    x = list(cur)
    for s in range(2 * m, n):
        D = N >> (s + 1)
        sig = cfg.shifts[s]
        for start in range(0, N, 2 * D):
            for j in range(D):
                i1 = start + j
                i2 = i1 + D
                ar, ai = x[i1]
                br, bi = x[i2]
                x[i1] = (round_shift(ar + br, sig),
                         round_shift(ai + bi, sig))
                dr, di = ar - br, ai - bi
                cr, ci = tw[(j << s) % N]
                pr, pi = complex_multiply_karatsuba(dr, di, cr, ci)
                sh = td + sig
                x[i2] = (round_shift(pr, sh), round_shift(pi, sh))
    got = [quantize_output(re, im, cfg.sample_decimal,
                           cfg.output_width, cfg.output_decimal)
           for re, im in x]
    exp = fft_fixed_batch_r22(samples, cfg)
    mism = [(k, got[k], exp[k]) for k in range(N) if got[k] != exp[k]]
    return samples, got, exp, mism


if __name__ == "__main__":
    from config import FFTConfig
    for N in (8, 16, 32, 64, 128):
        for inv in (False, True):
            cfg = FFTConfig(num_points=N, inverse=inv)
            _, got, exp, mism = run_stream(cfg)
            print(f"N={N:4d} inv={int(inv)}: "
                  f"{'BIT-EXACT' if not mism else f'MISMATCH x{len(mism)} first={mism[0]}'}")
