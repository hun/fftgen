"""Spike S5d: cycle-accurate streaming DIT R2^2 stage model (P7).

Mirror of _R22DIFStage: the DIT pair (A, A+1) merges into one 4-sample
group with THREE products on the inputs (multiply-then-combine):

  k in [0, H):    a0 arrives -> v0 = a0 << td -> vline (depth 3H)
  k in [H, 2H):   a1 arrives -> t1 = cmul(a1, T[2j*4^m'])  -> t1line (2H)
  k in [2H, 3H):  a2 arrives -> t2 = cmul(a2, T[j*4^m'])   -> t2line (H)
  k in [3H, 4H):  a3 arrives -> t3 = cmul(a3, T[3j*4^m']); combine:
                  y0 = round(v0 + t1 + t2 + t3, S)            pos j
                  y2 = round(v0 + t1 - t2 - t3, S)            pos j+2H
                  y1 = round(v0 - t1 + rot(t2) - rot(t3), S)  pos j+H
                  y3 = round(v0 - t1 - rot(t2) + rot(t3), S)  pos j+3H

Outputs emerge at position p + 3H (stage latency 3H): y0 at [3H,4H),
y1 via a H-deep queue at [4H,5H), y2 at [5H,6H), y3 at [6H,7H) (which
spills into the next block's [0,3H) window). Odd-n leaves DIT stage 0
(the trivial +/-1 stage) as a plain radix-2 DIT stage.
"""
import sys

sys.path.insert(0, "src")

from golden import fft_fixed_batch_r22_dit
from quant import complex_multiply_karatsuba, round_shift, quantize_output
from twiddles import canonical_twiddles


class R22DITStage:
    def __init__(self, A, n, N, sA, sB, td, tw, inverse=False):
        self.A = A
        self.n = n
        self.N = N
        self.sA, self.sB = sA, sB
        self.td = td
        self.H = 1 << A                      # group depth 2^A
        self.base = 4 ** ((n - 2 - A) // 2)  # twiddle stride 4^{m'}
        self.js = -1 if not inverse else 1
        self.tw = tw
        self.vline = [(0, 0)] * (3 * self.H)     # v0 = a0 << td
        self.t1line = [(0, 0)] * (2 * self.H)
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

    @property
    def latency(self):
        return 3 * self.H + 1        # +1: the registered output

    def _rot(self, z):
        return (-self.js * z[1], self.js * z[0])

    def _prod(self, z, t, sh):
        pr, pi = complex_multiply_karatsuba(z[0], z[1], t[0], t[1])
        return (round_shift(pr, sh), round_shift(pi, sh))

    def step(self, x, pos):
        H = self.H
        ret = self.out
        r, i = x
        k = pos % (4 * H)
        j = pos % H
        S = self.td + self.sA + self.sB
        cur = (0, 0)
        if k < H:
            # a0: v0 into the 3H line
            self.vline[self.vp] = (r << self.td, i << self.td)
            cur = self.q1[self.q1p]
        elif k < 2 * H:
            # a1: t1 into the 2H line
            self.t1line[self.t1p] = self._prod(
                (r, i), self.tw[(2 * j * self.base) % self.N], 0)
            cur = self.q2[self.q2p]
        elif k < 3 * H:
            # a2: t2 into the H line
            self.t2line[self.t2p] = self._prod(
                (r, i), self.tw[(j * self.base) % self.N], 0)
            cur = self.q3[self.q3p]
        else:
            # a3: t3 + the F4 combine
            t3 = self._prod((r, i), self.tw[(3 * j * self.base) % self.N], 0)
            v0 = self.vline[self.vp]
            t1 = self.t1line[self.t1p]
            t2 = self.t2line[self.t2p]
            r2 = self._rot(t2)
            r3 = self._rot(t3)
            y0 = (round_shift(v0[0] + t1[0] + t2[0] + t3[0], S),
                  round_shift(v0[1] + t1[1] + t2[1] + t3[1], S))
            y2 = (round_shift(v0[0] + t1[0] - t2[0] - t3[0], S),
                  round_shift(v0[1] + t1[1] - t2[1] - t3[1], S))
            y1 = (round_shift(v0[0] - t1[0] + r2[0] - r3[0], S),
                  round_shift(v0[1] - t1[1] + r2[1] - r3[1], S))
            y3 = (round_shift(v0[0] - t1[0] - r2[0] + r3[0], S),
                  round_shift(v0[1] - t1[1] - r2[1] + r3[1], S))
            self.q1[self.q1p] = y1
            self.q2[self.q2p] = y2
            self.q3[self.q3p] = y3
            cur = y0

        self.vp = (self.vp + 1) % (3 * H)
        self.t1p = (self.t1p + 1) % (2 * H)
        self.t2p = (self.t2p + 1) % H
        self.q1p = (self.q1p + 1) % H
        self.q2p = (self.q2p + 1) % (2 * H)
        self.q3p = (self.q3p + 1) % (3 * H)
        self.out = cur
        return ret


def run_stream(cfg, samples):
    N = cfg.num_points
    n = cfg.num_stages
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    td = cfg.twiddle_decimal
    stages = []
    lo = 0 if n % 2 == 0 else 1
    k = 0
    while lo + 2 * k + 1 < n:
        A = lo + 2 * k
        stages.append(R22DITStage(A, n, N, cfg.shifts[A], cfg.shifts[A + 1],
                                  td, tw, cfg.inverse))
        k += 1
    # leftover DIT stage 0 (odd n): plain radix-2 -- apply via the batch
    # for the model comparison (the streaming leftover comes with the RTL)
    from golden import _SDFStage
    lo_st = None
    if lo == 1:
        lo_st = _SDFStage(0, N, cfg.shifts[0], td,
                          [tw[(j << (n - 1)) % N] for j in range(1)],
                          dit=True)
    lat = (11 if lo_st else 0) + sum(st.latency for st in stages)
    # chain: the DIT leftover (stage 0) runs FIRST, then the pairs
    raw = []
    T = len(samples)
    for pos in range(T + lat):
        src = samples[pos] if pos < T else (0, 0)
        cur = src[:2]
        up = 0
        if lo_st is not None:
            cur = lo_st.step(cur[0], cur[1])
            up += 11
        for st in stages:
            cur = st.step(tuple(cur), pos - up)
            up += st.latency
        raw.append(cur)
    raw = raw[lat:]
    return [quantize_output(re, im, cfg.sample_decimal,
                            cfg.output_width, cfg.output_decimal)
            for re, im in raw]


if __name__ == "__main__":
    import random
    from config import FFTConfig

    def bitrev_order(N, n):
        return [int(format(k, f'0{n}b')[::-1], 2) for k in range(N)]

    ok = bad = 0
    for N in (4, 8, 16, 32, 64, 128, 256):
        for inv in (False, True):
            cfg = FFTConfig(num_points=N, inverse=inv,
                            input_order="bitreversed",
                            output_order="native")
            br = bitrev_order(N, cfg.num_stages)
            rng = random.Random(7)
            hi = 2 ** (cfg.sample_width - 1)
            raw = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
                   for _ in range(2 * N)]
            samples = [raw[f * N + br[j]]
                           for f in range(2) for j in range(N)]
            got = run_stream(cfg, samples)
            exp = []
            for f in range(2):
                fr = samples[f * N:(f + 1) * N]
                exp += fft_fixed_batch_r22_dit(fr, cfg)
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
