"""Spike S5i: R2^2 DIF stage with REGISTERED reads (L0 register stage).

The user's timing directive: every async LUTRAM read must land in a
register before it fans out. This RETIMES the L0-L5 pipeline by one:
the L1 now consumes the L0 REGISTERS (the reads captured at the
previous posedge), so the butterfly/combine of the a2/a3 arrives one
clock later and the s0/s1 pairing uses the sram (s0) + the s0_r L1
register (s1) at the combine.

Schedule per block (the a2/a3 captures at t, their L1 at t+1):
  sram/dram write at step 2D+g+1 (the a2's L1 = s0), address sp
  combine (y0/sd) at step 3D+g+2: s_r_r (s0, read at 3D+g+1)
      + s0_r (the L1 of 3D+g+1 = s1)
Products at the combine+1; pfifo at combine+4; the output after.
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
        # ---- L0 registers (the reads captured at the previous posedge)
        self.a_r = (0, 0)           # the ram read (a0 or a1)
        self.s_r = (0, 0)           # the sram read (s0 of the group)
        self.d_r = (0, 0)           # the dram read (d0)
        self.dl_r = (0, 0)          # the dline read (d1)
        self.x_r = (0, 0)           # the input
        self.t_r = (0, 0)           # the twiddle
        self.t_r2 = (0, 0)          # the twiddle delayed (aligns with L1 regs)
        self.g_r = 0                # the group of the L0 reads ((sp-1)%D)
        self.g_r2 = 0               # delayed (aligns with the L1 regs)
        # ---- L1 registers (the butterflies/combines)
        self.s0_r = (0, 0)          # the s_x (sram write / s1 at combine)
        self.d0_r = (0, 0)          # the d_x (dram/dline writes)
        self.sd_r = (0, 0)          # s0 - s1 (y2 operand)
        self.c1_r = (0, 0); self.c3_r = (0, 0)
        self.y0_raw_r = (0, 0)
        self.y0_raw2_r = (0, 0)
        self.y0_raw3_r = (0, 0)
        self.y0_raw4_r = (0, 0)
        # ---- L2/L3/L4
        self.prod_r = (0, 0)
        self.p_r = (0, 0)
        self.y0_r = (0, 0)
        self.shift_p_r = (0, 0)
        self.shift_p2_r = (0, 0)
        # ---- the phase chain (k1..k5; k6 = the delayed gate)
        self.k1 = 0
        self.k2 = 0
        self.k3 = 0
        self.k4 = 0
        self.k5 = 0
        self.k6 = 0
        self.k7 = 0
        self.pw_r = 0               # the product-window countdown (0 none)
        self.out = (0, 0)

    @property
    def latency(self):
        return 3 * self.D + 8

    def _rot(self, z):
        return (-self.js * z[1], self.js * z[0])

    def step(self, x, pos):
        D = self.D
        ret = self.out
        r, i = x
        k = pos % (4 * D)
        g = pos % D

        # ---- L5 writes (the t-1 registers, per-depth gates) ----
        #   ram     depth 0 (raw input)  gate k
        #   sram/   depth 0 (the CURRENT combinational s_x/d_x of the
        #   dram     step's L1 = the a2's s0/d0 at step 2D+g+1) gate k1
        #   dline   depth 0 (the current d_x = the a3's d1 at 3D+g+1)
        #   pfifo   depth 6 (shift_p)     gate k6
        #   mux     depth 6 (y0_r)        gate k6
        if k < 2 * D:
            self.ram[self.rp] = (r, i)
        # the L1 wire (s_x/d_x) is computed later in this step; the
        # sram/dram/dline writes sample the CURRENT step's L1, which is
        # the butterfly of the reads captured at t-1 = the phase k1.
        #   sram/dram at the a2's L1 step (k1 = the a2 phase)
        #   dline    at the a3's L1 step (k1 = the a3 phase)
        if 2 * D <= self.k1 < 3 * D:
            pass  # filled below, after the L1 compute (same step)
        # the pfifo write fires for the USED products: the operand was
        # selected at the k3 phase (the shift_p2 is 3 steps later); the
        # k5 register = the selection phase at this step. Used phases:
        # [0,D) y1, [D,2D) y3, [3D,4D) y2 -- the [2D,3D) c3 is waste.
        if self.k7 < 2 * D or self.k7 >= 3 * D:
            self.pfifo[self.pwp] = self.shift_p2_r
        out_val = self.y0_r if self.k7 >= 3 * D else self.pfifo[self.pr_r]

        # ---- the sram/dram/dline writes use THIS step's L1, so they
        # happen after the L1 compute below but before the captures ----

        cur_a = self.ram[self.rp]
        cur_s = self.sram[self.sp]
        cur_d = self.dram[self.sp]
        cur_dl = self.dline[self.sp]
        cur_x = (r, i)
        cur_k = k
        if k < D:
            cur_t = self.tw[(1 * g * self.base) % self.N]
        elif k < 2 * D:
            cur_t = self.tw[(3 * g * self.base) % self.N]
        else:
            cur_t = self.tw[(2 * g * self.base) % self.N]

        # ---- L1 (from the L0 REGISTERS: the reads at t-1) ----
        s_x = (round_shift(self.a_r[0] + self.x_r[0], self.sigma0),
               round_shift(self.a_r[1] + self.x_r[1], self.sigma0))
        d_x = (self.a_r[0] - self.x_r[0], self.a_r[1] - self.x_r[1])
        # the s1 (a3 butterfly) = the PREVIOUS step's L1 (s0_r); the s0
        # = the L0 s_r register (the sram read of the group). The
        # y0/sd combine (at step 3D+g+2) = s_r + s0_r.
        y0_raw = (self.s_r[0] + self.s0_r[0], self.s_r[1] + self.s0_r[1])
        sd = (self.s_r[0] - self.s0_r[0], self.s_r[1] - self.s0_r[1])
        c1 = (self.d_r[0] - self.js * self.dl_r[1],
              self.d_r[1] + self.js * self.dl_r[0])
        c3 = (self.d_r[0] + self.js * self.dl_r[1],
              self.d_r[1] - self.js * self.dl_r[0])

        # ---- the lag-D line writes (from the current L1) ----
        if 2 * D <= self.k1 < 3 * D:
            self.sram[self.sp] = s_x
            self.dram[self.sp] = d_x
        if self.k1 >= 3 * D:
            self.dline[self.sp] = d_x

        # ---- L2: products. The L1 regs at pre-edge t = the L1 of
        # step t-1 (the reads at t-2); the operand mux by k3 = the
        # phase of t-3 (the capture of the L1 regs' values) ----
        if self.k3 >= 3 * D:
            m = self.sd_r
            which = 2
        elif self.k3 < D:
            m = self.c1_r
            which = 1
        else:
            m = self.c3_r
            which = 3
        t = self.tw[(which * self.g_r2 * self.base) % self.N]
        prod = complex_multiply_karatsuba(m[0], m[1], t[0], t[1])

        # ---- L3: combine (from the t-1 products) ----
        p = self.prod_r

        # ---- L4: round_shift (select by k5 = the product's capture) ----
        if self.k5 >= 3 * D:
            sh = self.td + self.sigma1
        else:
            sh = self.td + self.sigma0 + self.sigma1
        shift_p = (round_shift(self.p_r[0], sh),
                   round_shift(self.p_r[1], sh))
        y0_s = (round_shift(self.y0_raw4_r[0], self.sigma1),
                round_shift(self.y0_raw4_r[1], self.sigma1))

        # ---- register updates (reverse-order chains) ----
        self.k7 = self.k6
        self.k6 = self.k5
        self.k5 = self.k4
        self.k4 = self.k3
        self.k3 = self.k2
        self.k2 = self.k1
        self.k1 = cur_k
        # (the pfifo write gates on the operand's selection phase via
        # k7 = the phase 3+4=7 steps back at the pre-edge: the k3-during
        # the L1 selection of the product now in the shift_p2)
        self.a_r = cur_a
        self.s_r = cur_s
        self.d_r = cur_d
        self.dl_r = cur_dl
        self.x_r = cur_x
        self.t_r2 = self.t_r
        self.t_r = cur_t
        self.g_r2 = self.g_r
        self.g_r = (self.sp - 1) % D
        self.s0_r = s_x
        self.d0_r = d_x
        self.sd_r = sd
        self.c1_r = c1
        self.c3_r = c3
        self.y0_raw4_r = self.y0_raw3_r
        self.y0_raw3_r = self.y0_raw2_r
        self.y0_raw2_r = self.y0_raw_r
        self.y0_raw_r = y0_raw
        self.prod_r = prod
        self.p_r = p
        self.y0_r = y0_s
        self.shift_p2_r = self.shift_p_r
        self.shift_p_r = shift_p
        self.rp = (self.rp + 1) % (2 * D)
        self.sp = (self.sp + 1) % D
        self.pwp = (self.pwp + 1) % (2 * D)
        self.pr_r = (self.pwp - D) % (2 * D)
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
        D0 = N >> (s + 1)
        sig = cfg.shifts[s]
        for start in range(0, len(x), 2 * D0):
            for j in range(D0):
                i1 = start + j
                i2 = i1 + D0
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
    for N in (4, 8, 16, 32, 64):
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