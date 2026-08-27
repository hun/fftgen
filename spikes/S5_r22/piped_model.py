"""Spike S5g: PIPELINED R2^2 DIF stage model (timing closure).

WIP: the L0-L5 structure is right but NOT yet bit-exact -- the
y0/product output alignment still has an off-by-one; needs a
dedicated debugging session (mirror the per-depth phase gates
and the pfifo lag exactly).

Mirrors the planned pipelined fft_stage_r22 register-for-register:
the step() is the posedge -- L5 writes use the t-1 pipeline registers,
L0 captures the combinational reads, L1..L4 compute the next-stage
registers. The values are identical to the unpipelined model.
"""
import sys

sys.path.insert(0, "src")

from quant import complex_multiply_karatsuba, round_shift
from twiddles import canonical_twiddles


class R22DIFStagePiped:
    def __init__(self, m, N, sigma0, sigma1, td, tw, inverse=False):
        self.m = m
        self.N = N
        self.sigma0, self.sigma1 = sigma0, sigma1
        self.td = td
        self.D = N >> (2 * m + 2)
        self.base = 4 ** m
        self.js = -1 if not inverse else 1
        self.tw = tw
        self.ram = [(0, 0)] * (2 * self.D)
        self.sram = [(0, 0)] * self.D
        self.dram = [(0, 0)] * self.D
        self.dline = [(0, 0)] * self.D
        self.pfifo = [(0, 0)] * (2 * self.D)
        self.rp = 0
        self.sp = 0
        self.pwp = 0
        self.pr_r = 0
        # pipeline registers (the t-1 values are used by the t writes)
        self.s0_r = (0, 0); self.d0_r = (0, 0)
        self.s1_r = (0, 0); self.d1_r = (0, 0)
        self.sd_r = (0, 0)
        self.c1_r = (0, 0); self.c3_r = (0, 0)
        self.t_r = (0, 0)
        self.prod_r = (0, 0)
        self.p_r = (0, 0)
        self.y0_raw_r = (0, 0)      # raw sum (L1, s_r + s_x)
        self.y0_raw2_r = (0, 0)     # delayed (L2)
        self.y0_raw3_r = (0, 0)     # delayed (L3)
        self.y0_r = (0, 0)          # rounded sum path (L4)
        self.shift_p_r = (0, 0)     # rounded product (L4)
        self.k1 = 0                 # capture phase delayed to L2
        self.k2 = 0
        self.k3 = 0
        self.k4 = 0                 # capture phase delayed to L5
        self.out = (0, 0)

    @property
    def latency(self):
        return 3 * self.D + 6

    def _rot(self, z):
        return (-self.js * z[1], self.js * z[0])

    def step(self, x, pos):
        D = self.D
        ret = self.out
        r, i = x
        k = pos % (4 * D)
        g = pos % D

        # ---- L5: writes + output. Each write uses its register at its
        # own pipeline depth, gated by the matching delayed phase:
        #   ram write   depth 0 (raw input)   gate k
        #   sram/dram   depth 1 (s0_r/d0_r)   gate k1
        #   dline       depth 1 (d1_r)        gate k1
        #   pfifo       depth 4 (shift_p_r)   gate k4
        #   output mux  depth 4 (y0_r)        gate k4
        if k < 2 * D:
            self.ram[self.rp] = (r, i)
        if self.k1 < 2 * D:
            self.pfifo[self.pwp] = self.shift_p_r
        if 2 * D <= self.k1 < 3 * D:
            self.sram[self.sp] = self.s0_r
            self.dram[self.sp] = self.d0_r
        if self.k1 >= 3 * D:
            self.dline[self.sp] = self.d1_r
        if self.k4 >= 3 * D:
            self.pfifo[self.pwp] = self.shift_p_r
        out_val = self.y0_r if self.k4 >= 3 * D else self.pfifo[self.pr_r]

        # ---- L0 capture (combinational reads at t) ----
        a_r = self.ram[self.rp]
        s_r = self.sram[self.sp]
        d_r = self.dram[self.sp]
        dl_r = self.dline[self.sp]
        x_r = (r, i)
        k_r = k
        if k < D:
            t_r = self.tw[(1 * g * self.base) % self.N]
        elif k < 2 * D:
            t_r = self.tw[(3 * g * self.base) % self.N]
        else:
            t_r = self.tw[(2 * g * self.base) % self.N]

        # ---- L1: butterfly + combine (on the L0 captures) ----
        s_x = (round_shift(a_r[0] + x_r[0], self.sigma0),
               round_shift(a_r[1] + x_r[1], self.sigma0))
        d_x = (a_r[0] - x_r[0], a_r[1] - x_r[1])
        sd = (s_r[0] - s_x[0], s_r[1] - s_x[1])
        c1 = (d_r[0] - self.js * dl_r[1], d_r[1] + self.js * dl_r[0])
        c3 = (d_r[0] + self.js * dl_r[1], d_r[1] - self.js * dl_r[0])
        # y0 = s0 + s1 with the s0 from the SRAM read capture (s_r) and
        # s1 = the current butterfly s_x (the a3 clock's)
        y0_raw = (s_r[0] + s_x[0], s_r[1] + s_x[1])

        # ---- L2: products (operand mux by the t-1 capture phase) ----
        if self.k1 >= 3 * D:
            m = self.sd_r
        elif self.k1 < D:
            m = self.c1_r
        else:
            m = self.c3_r
        t = self.t_r
        prod = complex_multiply_karatsuba(m[0], m[1], t[0], t[1])

        # ---- L3: combine (from the t-1 products) ----
        p = self.prod_r

        # ---- L4: round_shift (from the t-1 p and the delayed y0) ----
        if self.k1 >= 3 * D:
            sh = self.td + self.sigma1
        else:
            sh = self.td + self.sigma0 + self.sigma1
        shift_p = (round_shift(self.p_r[0], sh),
                   round_shift(self.p_r[1], sh))
        y0_s = (round_shift(self.y0_raw3_r[0], self.sigma1),
                round_shift(self.y0_raw3_r[1], self.sigma1))

        # ---- register updates ----
        self.s0_r = s_x; self.d0_r = d_x
        self.s1_r = s_x; self.d1_r = d_x
        self.sd_r = sd
        self.c1_r = c1; self.c3_r = c3
        self.t_r = t_r
        self.k1 = k_r
        self.k2 = self.k1
        self.k3 = self.k2
        self.k4 = self.k3
        self.prod_r = prod
        self.p_r = p
        self.y0_raw_r = y0_raw
        self.y0_raw2_r = self.y0_raw_r
        self.y0_raw3_r = self.y0_raw2_r
        self.y0_r = y0_s
        self.shift_p_r = shift_p
        self.rp = (self.rp + 1) % (2 * D)
        self.sp = (self.sp + 1) % D
        self.pwp = (self.pwp + 1) % (2 * D)
        self.pr_r = (self.pwp + 1 - D - 1) % (2 * D)
        self.out = out_val
        return ret


def run_piped(cfg, samples):
    from golden import fft_fixed_batch_r22
    from quant import quantize_output
    N = cfg.num_points
    n = cfg.num_stages
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    td = cfg.twiddle_decimal
    stages = []
    m = 0
    while 2 * m + 1 < n:
        stages.append(R22DIFStagePiped(m, N, cfg.shifts[2 * m],
                                       cfg.shifts[2 * m + 1], td, tw,
                                       cfg.inverse))
        m += 1
    raw = []
    T = len(samples)
    lat = sum(st.latency for st in stages)
    for pos in range(T + lat):
        src = samples[pos] if pos < T else (0, 0)
        cur = src[:2]
        up = 0
        for st in stages:
            cur = st.step(tuple(cur), pos - up)
            up += st.latency
        raw.append(cur)
    raw = raw[lat:]
    x = [list(v) for v in raw]
    for s in range(2 * len(stages), n):
        D = N >> (s + 1)
        sig = cfg.shifts[s]
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
    got = [quantize_output(re, im, cfg.sample_decimal,
                           cfg.output_width, cfg.output_decimal)
           for re, im in x]
    exp = []
    for f in range(len(samples) // N):
        exp += fft_fixed_batch_r22(samples[f * N:(f + 1) * N], cfg)
    return got, exp


if __name__ == "__main__":
    import random
    from config import FFTConfig
    ok = bad = 0
    for N in (4, 8, 16, 32, 64, 128):
        for inv in (False, True):
            cfg = FFTConfig(num_points=N, inverse=inv)
            rng = random.Random(7)
            hi = 2 ** (cfg.sample_width - 1)
            samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
                       for _ in range(2 * N)]
            got, exp = run_piped(cfg, samples)
            if got == exp:
                ok += 1
            else:
                bad += 1
                mm = next((k for k in range(len(got))
                           if got[k] != exp[k]), None)
                print(f'N={N} inv={int(inv)}: MISMATCH at {mm}: '
                      f'{got[mm]} vs {exp[mm]}')
    print(f'{ok} bit-exact, {bad} mismatched')
