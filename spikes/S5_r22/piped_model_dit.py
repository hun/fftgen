"""Spike S5h: PIPELINED R2^2 DIT stage model (timing closure mirror).

Multiply-then-combine DIT with the pipeline depths:
  L0 capture:  line reads (vline/t1line/t2line) + input + twiddle
  L1:          v0 = a0 << td (v0_r); product operands (x_r, t_r)
  L2:          product (prod_r, the DSP MREG); the v/t1/t2 line
               writes (gate k2, the k2 = the capture phase delayed 2)
  L3:          F4 combine (v0d1/t1d1/t2d1 + the t3 product prod2_r,
               all depth 2) + round -> y*_r
  L4:          q1/q2/q3 writes + output mux (gate k4, depth 3)

Line sizes change: the t1line drops to H (write at a1+2, read at
a3+2, lag H); the vline stays 3H (write at a0+2, read at a3+2, lag
3H). The q writes are 4 clocks after the a3: address (q*p - 4) mod
size. The stage latency is 3H+5.
"""
import sys

sys.path.insert(0, "src")

from quant import complex_multiply_karatsuba, round_shift


class R22DITStagePiped:
    def __init__(self, A, n, N, sA, sB, td, tw, inverse=False):
        self.A = A
        self.n = n
        self.N = N
        self.sA, self.sB = sA, sB
        self.td = td
        self.H = 1 << A
        self.base = 4 ** ((n - 2 - A) // 2)
        self.js = -1 if not inverse else 1
        self.tw = tw
        self.vline = [(0, 0)] * (3 * self.H)
        self.t1line = [(0, 0)] * self.H       # size H (lag H, +2 write)
        self.t2line = [(0, 0)] * self.H
        self.q1 = [(0, 0)] * self.H
        self.q2 = [(0, 0)] * (2 * self.H)
        self.q3 = [(0, 0)] * (3 * self.H)
        self.out = (0, 0)
        self.vp = 0
        self.t1p = 0
        self.t2p = 0
        self.q1p = 0
        self.q2p = 0
        self.q3p = 0
        # pipeline registers
        self.x_r = (0, 0)
        self.t_r = (0, 0)
        self.v0_r = (0, 0)          # L1: a0 << td
        self.prod_r = (0, 0)        # L2: cmul(x_r, t_r) (the MREG)
        self.prod2_r = (0, 0)       # L3: the t3 at the combine
        # combine input delays (L0 reads delayed one clock)
        self.v0d1_r = (0, 0)
        self.t1d1_r = (0, 0)
        self.t2d1_r = (0, 0)
        self.y0_r = (0, 0)
        self.y1_r = (0, 0)
        self.y2_r = (0, 0)
        self.y3_r = (0, 0)
        self.k1 = 0
        self.k2 = 0
        self.k3 = 0
        self.k4 = 0

    @property
    def latency(self):
        return 3 * self.H + 5

    def _rot(self, z):
        return (-self.js * z[1], self.js * z[0])

    def step(self, x, pos):
        H = self.H
        ret = self.out
        r, i = x
        k = pos % (4 * H)
        j = pos % H
        S = self.td + self.sA + self.sB

        # ---- L4 writes + output (per-depth gates) ----
        if self.k4 >= 3 * H:
            self.q1[self.q1p] = self.y1_r
            self.q2[self.q2p] = self.y2_r
            self.q3[self.q3p] = self.y3_r
        # the output register adds 1, so the mux branches by the phase
        # of the NEXT clock: (k-4) mod 4H (the queue lags self-align)
        kb = (k - 4) % (4 * H)
        if self.k4 >= 3 * H:
            out_val = self.y0_r
        elif kb < H:
            out_val = self.q1[self.q1p]
        elif kb < 2 * H:
            out_val = self.q2[self.q2p]
        else:
            out_val = self.q3[self.q3p]

        # ---- L0 capture (reads + input + twiddle) ----
        v_r = self.vline[self.vp]
        t1_r = self.t1line[self.t1p]
        t2_r = self.t2line[self.t2p]
        x_r = (r, i)
        k_r = k
        if k < H:
            t_r = ((1 << self.td) - 1, 0)   # W^0 quantized; unused
        elif k < 2 * H:
            t_r = self.tw[(2 * j * self.base) % self.N]
        elif k < 3 * H:
            t_r = self.tw[(j * self.base) % self.N]
        else:
            t_r = self.tw[(3 * j * self.base) % self.N]

        # ---- L1: the a0 shift + the product operands ----
        v0 = (r << self.td, i << self.td)

        # ---- L2: the product (from the t-1 operands) ----
        prod = complex_multiply_karatsuba(self.x_r[0], self.x_r[1],
                                          self.t_r[0], self.t_r[1])
        # the v/t1/t2 line writes. The v0_r is depth 1 (gate k1, and
        # the write is one clock later than the read's address: target
        # (vp+1) mod 3H); the prod_r is depth 2 (gate k2, no fix).
        if self.k1 < H:
            self.vline[(self.vp + 1) % (3 * H)] = self.v0_r
        if H <= self.k2 < 2 * H:
            self.t1line[self.t1p] = self.prod_r
        if 2 * H <= self.k2 < 3 * H:
            self.t2line[self.t2p] = self.prod_r

        # ---- L3: the F4 combine (the t-1 reads + the t-1 t3) ----
        v0c = self.v0d1_r
        t1c = self.t1d1_r
        t2c = self.t2d1_r
        t3 = self.prod2_r
        r2 = self._rot(t2c)
        r3 = self._rot(t3)
        y0 = (round_shift(v0c[0] + t1c[0] + t2c[0] + t3[0], S),
              round_shift(v0c[1] + t1c[1] + t2c[1] + t3[1], S))
        y2 = (round_shift(v0c[0] + t1c[0] - t2c[0] - t3[0], S),
              round_shift(v0c[1] + t1c[1] - t2c[1] - t3[1], S))
        y1 = (round_shift(v0c[0] - t1c[0] + r2[0] - r3[0], S),
              round_shift(v0c[1] - t1c[1] + r2[1] - r3[1], S))
        y3 = (round_shift(v0c[0] - t1c[0] - r2[0] + r3[0], S),
              round_shift(v0c[1] - t1c[1] - r2[1] + r3[1], S))

        # ---- register updates (reverse-order chains) ----
        self.k4 = self.k3
        self.k3 = self.k2
        self.k2 = self.k1
        self.k1 = k_r
        self.x_r = x_r
        self.t_r = t_r
        self.v0_r = v0
        self.prod2_r = self.prod_r     # old first (the t3 at depth 2)
        self.prod_r = prod
        self.v0d1_r = v_r
        self.t1d1_r = t1_r
        self.t2d1_r = t2_r
        self.y0_r = y0
        self.y1_r = y1
        self.y2_r = y2
        self.y3_r = y3
        self.vp = (self.vp + 1) % (3 * H)
        self.t1p = (self.t1p + 1) % H
        self.t2p = (self.t2p + 1) % H
        self.q1p = (self.q1p + 1) % H
        self.q2p = (self.q2p + 1) % (2 * H)
        self.q3p = (self.q3p + 1) % (3 * H)
        self.out = out_val
        return ret


def run_piped_dit(cfg, samples):
    from golden import fft_fixed_batch_r22_dit
    from quant import quantize_output
    from twiddles import canonical_twiddles
    N = cfg.num_points
    n = cfg.num_stages
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    td = cfg.twiddle_decimal
    lo = 0 if n % 2 == 0 else 1
    stages = []
    k = 0
    while lo + 2 * k + 1 < n:
        A = lo + 2 * k
        stages.append(R22DITStagePiped(A, n, N, cfg.shifts[A],
                                       cfg.shifts[A + 1], td, tw,
                                       cfg.inverse))
        k += 1
    # odd n: DIT stage 0 (the trivial +/-1 stage) runs FIRST as a
    # streaming plain radix-2 SDF stage (mirror of the verified model)
    leftover = None
    if lo == 1:
        from golden import _SDFStage
        leftover = _SDFStage(0, N, cfg.shifts[0], td, [tw[0]], dit=True)
    raw = []
    T = len(samples)
    lat = (11 if leftover is not None else 0) \
        + sum(st.latency for st in stages)
    for pos in range(T + lat):
        src = samples[pos] if pos < T else (0, 0)
        cur = src[:2]
        up = 0
        if leftover is not None:
            cur = leftover.step(cur[0], cur[1])
            up += 11
        for st in stages:
            cur = st.step(tuple(cur), pos - up)
            up += st.latency
        raw.append(cur)
    raw = raw[lat:]
    got = [quantize_output(re, im, cfg.sample_decimal,
                           cfg.output_width, cfg.output_decimal)
           for re, im in raw]
    exp = []
    for f in range(len(samples) // N):
        exp += fft_fixed_batch_r22_dit(samples[f * N:(f + 1) * N], cfg)
    return got, exp


if __name__ == "__main__":
    import random
    from config import FFTConfig
    ok = bad = 0
    for N in (4, 8, 16, 32, 64):
        for inv in (False, True):
            cfg = FFTConfig(num_points=N, inverse=inv,
                            input_order="bitreversed")
            rng = random.Random(7)
            hi = 2 ** (cfg.sample_width - 1)
            samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
                       for _ in range(2 * N)]
            got, exp = run_piped_dit(cfg, samples)
            if got == exp:
                ok += 1
            else:
                bad += 1
                mm = next((k for k in range(len(got))
                           if got[k] != exp[k]), None)
                assert mm is not None
                print(f'N={N} inv={int(inv)}: MISMATCH at {mm}: '
                      f'{got[mm]} vs {exp[mm]}')
    print(f'{ok} bit-exact, {bad} mismatched')
