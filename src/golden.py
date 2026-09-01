"""Bit-accurate golden models for the FFT core (PLAN.md section 3).

Three model levels, deliberately independent implementations:

* :class:`FFTFloatReference` -- ideal DFT/IDFT in double precision (L0).
  Used for SQNR measurement and sanity; never bit-compared to hardware.

* :func:`fft_fixed_batch` -- fixed-point batch reference written as a
  straightforward in-place Cooley-Tukey loop nest (L1a). Shares only the
  canonical quantization primitives from :mod:`quant`; its schedule
  (block loops) is intentionally different from the streaming model's.

* :class:`SDFGoldenModel` -- cycle-accurate streaming model of the planned
  R2-SDF pipeline (L1b): one stage object per radix-2 stage, each with its
  delay line, alternating COMPUTE / PASS-FILL phases, per-stage shifts and
  the combined multiply-path rounding shift. Mirrors the future RTL
  register-by-register (see PLAN.md appendix A) and supports arbitrary
  freeze gaps via ``tick(False, ...)``.

The primary verification axis is ``SDFGoldenModel`` vs. ``fft_fixed_batch``
**bit-exact equality** (schedule equivalence), with both anchored to the
float reference by SQNR bounds.
"""

from collections import deque
import cmath
import math
from typing import List, Optional, Sequence, Tuple

from config import FFTConfig
from quant import (complex_multiply_karatsuba, quantize_output,
                   round_shift, saturate)
from twiddles import canonical_twiddles

Complex = Tuple[int, int]


# ----------------------------------------------------------------------
# L0: float reference
# ----------------------------------------------------------------------

def fft_float_reference(samples: Sequence[complex], inverse: bool = False
                        ) -> List[complex]:
    """Ideal DFT/IDFT (no 1/N on either direction), direct sum, O(N^2).

    Deliberately naive: independent of every other implementation here.
    """
    N = len(samples)
    sign = 1.0 if inverse else -1.0
    out = []
    for k in range(N):
        acc = 0j
        for n, x in enumerate(samples):
            acc += x * cmath.exp(sign * 2j * math.pi * k * n / N)
        out.append(acc)
    return out


def fft_float_radix2(samples: Sequence[complex], inverse: bool = False
                     ) -> List[complex]:
    """Float DFT via iterative Cooley-Tukey (O(N log N), for large-N tests)."""
    N = len(samples)
    if N & (N - 1):
        raise ValueError("N must be a power of two")
    a = [complex(x) for x in samples]
    # bit-reverse permutation
    j = 0
    for i in range(1, N):
        bit = N >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    sign = 1.0 if inverse else -1.0
    length = 2
    while length <= N:
        ang = sign * 2 * math.pi / length
        wlen = complex(math.cos(ang), math.sin(ang))
        for start in range(0, N, length):
            w = 1 + 0j
            half = length // 2
            for k in range(start, start + half):
                u = a[k]
                v = a[k + half] * w
                a[k] = u + v
                a[k + half] = u - v
                w *= wlen
        length <<= 1
    return a


# ----------------------------------------------------------------------
# L1a: fixed-point batch reference (independent schedule)
# ----------------------------------------------------------------------

def fft_fixed_batch(samples: Sequence[Complex], cfg: FFTConfig,
                    twiddles: Optional[Sequence[Complex]] = None
                    ) -> List[Complex]:
    """Fixed-point DIF batch reference, natural-in / bit-reversed-out.

    Requires native input ordering and the default output order of the DIF
    pipeline (the streaming model's contract); order conversion is a stream
    permutation handled outside the transform.

    Quantization points (must match PLAN.md appendix A):
      sum path   : exact add, then round_shift(sigma_s)
      mult path  : exact Karatsuba products, then ONE
                   round_shift(twiddle_decimal + sigma_s)
    Every stage multiplies (uniform datapath; the radix-2^2 folding that
    makes some stages multiplier-free is an RTL resource optimization whose
    numerical equivalence must be proven separately).
    """
    if cfg.ssr != 1:
        raise NotImplementedError("batch reference supports ssr=1 only")
    if cfg.is_dit:
        raise NotImplementedError("batch reference is DIF-only")
    N = cfg.num_points
    n = cfg.num_stages
    shifts = cfg.shifts
    td = cfg.twiddle_decimal
    if twiddles is None:
        twiddles = canonical_twiddles(N, cfg.twiddle_width,
                                      cfg.twiddle_decimal, cfg.inverse)

    x: List[List[int]] = [[re, im] for re, im in samples]

    for s in range(n):
        D = N >> (s + 1)
        sig = shifts[s]
        for start in range(0, N, 2 * D):
            for j in range(D):
                i1 = start + j
                i2 = i1 + D
                ar, ai = x[i1]
                br, bi = x[i2]
                # sum path
                x[i1] = [round_shift(ar + br, sig),
                         round_shift(ai + bi, sig)]
                # difference path (every stage multiplies -- uniform)
                dr, di = ar - br, ai - bi
                cr, ci = twiddles[(j << s) % N]
                pr, pi = complex_multiply_karatsuba(dr, di, cr, ci)
                sh = td + sig
                x[i2] = [round_shift(pr, sh), round_shift(pi, sh)]

    frac_final = cfg.sample_decimal   # datapath stays Q(sample_decimal):
    # adds are format-aligned, the multiply path's combined shift removes
    # exactly the twiddle fractional bits (see appendix A). With the
    # conservative schedule the reported spectrum is X_true/N.
    return [quantize_output(re, im, frac_final,
                            cfg.output_width, cfg.output_decimal)
            for re, im in x]


def fft_fixed_batch_r22(samples: Sequence[Complex], cfg: FFTConfig,
                        twiddles: Optional[Sequence[Complex]] = None
                        ) -> List[Complex]:
    """Fixed-point DIF batch reference, **radix-2² contract** (P7).

    The radix-2² folding merges each stage pair (2m, 2m+1) into one
    4-sample group with THREE products instead of four:

        b0 = round(a0+a2, s_{2m});   b1 = round(a1+a3, s_{2m})
        d0 = a0-a2;                  d1 = a1-a3          (exact)
        y0 = round(b0+b1, s_{2m+1})
        y2 = round(cmul(b0-b1, T[2j·4^m]),  td + s_{2m+1})
        y1 = round(cmul(d0 -/+ j·d1, T[j·4^m]),   td + s_{2m}+s_{2m+1})
        y3 = round(cmul(d0 +/- j·d1, T[3j·4^m]),  td + s_{2m}+s_{2m+1})

    The ±j diff combine is EXACT in the canonical table (rotation
    identity T[i+N/4] = ∓j·T[i] holds bit-exactly, magnitude-first
    quantization -- verified in spikes/S5_r22/). The product paths use
    ONE fused rounding at td+s_{2m}+s_{2m+1}; the sum paths keep the two
    sub-stage roundings. This contract differs from
    :func:`fft_fixed_batch` by at most 1 LSB (rounding placement) with
    identical SQNR -- it is the RE-PINNED golden for the R2² RTL (P7).

    Group geometry: 4^m blocks of size N/4^m, each split into groups
    (j, j+D, j+2D, j+3D), D = N/4^{m+1}, twiddle stride 4^m. Odd stage
    counts leave the last stage as plain radix-2.
    """
    if cfg.ssr != 1:
        raise NotImplementedError("batch reference supports ssr=1 only")
    if cfg.is_dit:
        raise NotImplementedError("batch R2² reference is DIF-only")
    N = cfg.num_points
    n = cfg.num_stages
    shifts = cfg.shifts
    td = cfg.twiddle_decimal
    if twiddles is None:
        twiddles = canonical_twiddles(N, cfg.twiddle_width,
                                      cfg.twiddle_decimal, cfg.inverse)
    js = -1 if not cfg.inverse else 1     # W^{N/4} = -j fwd, +j inv

    x: List[List[int]] = [[re, im] for re, im in samples]

    m = 0
    while 2 * m + 1 < n:
        s0, s1 = shifts[2 * m], shifts[2 * m + 1]
        D = N >> (2 * m + 2)              # group depth N/4^{m+1}
        base = 4 ** m                     # twiddle stride 4^m
        for b in range(base):             # 4^m blocks of size N/4^m
            off = b * (N // base)
            for j in range(D):
                a0r, a0i = x[off + j]
                a1r, a1i = x[off + j + D]
                a2r, a2i = x[off + j + 2 * D]
                a3r, a3i = x[off + j + 3 * D]
                # sub-stage 2m: sums rounded at s_{2m}, diffs exact
                b0 = [round_shift(a0r + a2r, s0),
                      round_shift(a0i + a2i, s0)]
                b1 = [round_shift(a1r + a3r, s0),
                      round_shift(a1i + a3i, s0)]
                d0r, d0i = a0r - a2r, a0i - a2i
                d1r, d1i = a1r - a3r, a1i - a3i
                # sub-stage 2m+1: three products, one rounding each
                x[off + j] = [round_shift(b0[0] + b1[0], s1),
                              round_shift(b0[1] + b1[1], s1)]
                cr, ci = twiddles[(2 * j * base) % N]
                pr, pi = complex_multiply_karatsuba(
                    b0[0] - b1[0], b0[1] - b1[1], cr, ci)
                x[off + j + D] = [round_shift(pr, td + s1),
                                  round_shift(pi, td + s1)]
                cr, ci = twiddles[(j * base) % N]
                pr, pi = complex_multiply_karatsuba(
                    d0r - js * d1i, d0i + js * d1r, cr, ci)
                x[off + j + 2 * D] = [round_shift(pr, td + s0 + s1),
                                      round_shift(pi, td + s0 + s1)]
                cr, ci = twiddles[(3 * j * base) % N]
                pr, pi = complex_multiply_karatsuba(
                    d0r + js * d1i, d0i - js * d1r, cr, ci)
                x[off + j + 3 * D] = [round_shift(pr, td + s0 + s1),
                                      round_shift(pi, td + s0 + s1)]
        m += 1
    # leftover last stage (odd stage count): plain radix-2
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
                cr, ci = twiddles[(j << s) % N]
                pr, pi = complex_multiply_karatsuba(dr, di, cr, ci)
                sh = td + sig
                x[i2] = [round_shift(pr, sh), round_shift(pi, sh)]

    return [quantize_output(re, im, cfg.sample_decimal,
                            cfg.output_width, cfg.output_decimal)
            for re, im in x]


# ----------------------------------------------------------------------
# P7: cycle-accurate streaming R2² DIF model (verified vs the batch
# contract, spikes/S5_r22/stream_model.py)
# ----------------------------------------------------------------------

class _R22DIFStage:
    """One radix-2² DIF stage (stage pair (2m, 2m+1) merged).

    Group depth D = N/4^{m+1}, twiddle stride 4^m, block size 4D. Per
    4D-clock block the stream carries D groups (a0..a3 at positions
    g, g+D, g+2D, g+3D).

      k in [0, 2D):    a0/a1 raw store into ram (lag-2D ring)
      k in [2D, 3D):   a2 arrives -> (a0,a2): s0/d0 into sram/dram
      k in [3D, 4D):   a3 arrives -> (a1,a3): s1/d1; s0,s1 meet ->
                       y0 out; y2 product -> pfifo; d1 into dline

    ONE shared complex multiplier computes the three products at
    staggered clocks (consecutive groups would collide otherwise):

      k in [3D, 4D):  y2 = cmul(s0-s1, T[2g*4^m])   (data ready now)
      k in [0, D):    y1 = cmul(d0 -j*d1, T[g*4^m])  (next block, +D)
      k in [D, 2D):   y3 = cmul(d0 +j*d1, T[3g*4^m]) (next block, +2D)

    The d0/d1 operands are re-read from dram/dline at the staggered
    clocks: the read-old at the current sp returns d0(g)/d1(g) for all
    three clocks (the next write to that address is the next block's).
    Each product is staged D clocks in the pfifo (lag D), so position
    p's value emerges at clock p + 3D (stage latency 3D): the output
    mux emits y0 at k in [3D, 4D) and the pfifo read otherwise.
    """

    def __init__(self, m: int, N: int, sigma0: int, sigma1: int,
                 td: int, tw: Sequence[Complex], inverse: bool = False):
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
        self.out = (0, 0)
        self.rp = 0
        self.sp = 0
        self.pwp = 0
        self.pr_r = 0               # registered pfifo read address

    @property
    def latency(self) -> int:
        return 3 * self.D + 1       # +1: the registered output register

    def _prod(self, z: Complex, t: Complex, sh: int) -> Complex:
        pr, pi = complex_multiply_karatsuba(z[0], z[1], t[0], t[1])
        return (round_shift(pr, sh), round_shift(pi, sh))

    def step(self, x: Complex, pos: int) -> Complex:
        D = self.D
        ret = self.out              # registered output: value of LAST clock
        r, i = x
        k = pos % (4 * D)
        g = pos % D
        cur = (0, 0)

        if k < 2 * D:
            # a0/a1 raw store; staggered products y1 (k<D) / y3 (k>=D)
            self.ram[self.rp] = (r, i)
            d0 = self.dram[self.sp]
            d1 = self.dline[self.sp]
            S = self.td + self.sigma0 + self.sigma1
            if k < D:
                cm = (d0[0] - self.js * d1[1], d0[1] + self.js * d1[0])
                y = self._prod(cm, self.tw[(g * self.base) % self.N], S)
            else:
                cp = (d0[0] + self.js * d1[1], d0[1] - self.js * d1[0])
                y = self._prod(cp, self.tw[(3 * g * self.base) % self.N], S)
            self.pfifo[self.pwp] = y
            cur = self.pfifo[self.pr_r]
        elif k < 3 * D:
            # a2 arrives: (a0, a2) -> s0/d0 into the lag-D lines
            a0 = self.ram[self.rp]
            self.sram[self.sp] = (round_shift(a0[0] + r, self.sigma0),
                                  round_shift(a0[1] + i, self.sigma0))
            self.dram[self.sp] = (a0[0] - r, a0[1] - i)
            cur = self.pfifo[self.pr_r]
        else:
            # a3 arrives: (a1, a3) -> s1/d1; s0,s1 meet -> y0, y2
            a1 = self.ram[self.rp]
            s1 = (round_shift(a1[0] + r, self.sigma0),
                  round_shift(a1[1] + i, self.sigma0))
            d1 = (a1[0] - r, a1[1] - i)
            s0 = self.sram[self.sp]
            y0 = (round_shift(s0[0] + s1[0], self.sigma1),
                  round_shift(s0[1] + s1[1], self.sigma1))
            y2 = self._prod((s0[0] - s1[0], s0[1] - s1[1]),
                            self.tw[(2 * g * self.base) % self.N],
                            self.td + self.sigma1)
            self.pfifo[self.pwp] = y2
            self.dline[self.sp] = d1
            cur = y0

        self.rp = (self.rp + 1) % (2 * D)
        self.sp = (self.sp + 1) % D
        self.pwp = (self.pwp + 1) % (2 * D)
        self.pr_r = (self.pwp - D) % (2 * D)
        self.out = cur
        return ret


class R22SDFGoldenModel:
    """Cycle-accurate streaming model of the R2² DIF pipeline (P7).

    One :class:`_R22DIFStage` per stage pair; the leftover last stage
    (odd stage count, the trivial W^0 stage) is applied as a per-frame
    batch post-process (its streaming FSM/preload arrives with the R2²
    stage RTL). Verified bit-exact against :func:`fft_fixed_batch_r22`
    (the re-pinned contract) over N = 8..1024, fwd+inv, widths,
    scaling schedules and multi-frame streams.
    """

    def __init__(self, cfg: FFTConfig):
        if cfg.ssr != 1:
            raise NotImplementedError("R22 model supports ssr=1 only")
        if cfg.is_dit or cfg.input_order != "native" \
                or cfg.output_order != "bitreversed":
            raise NotImplementedError(
                "R22 model covers native->bitreversed only")
        self.cfg = cfg
        N = cfg.num_points
        self.N = N
        n = cfg.num_stages
        td = cfg.twiddle_decimal
        tw = canonical_twiddles(N, cfg.twiddle_width, td, cfg.inverse)
        self.stages = []
        m = 0
        while 2 * m + 1 < n:
            self.stages.append(_R22DIFStage(
                m, N, cfg.shifts[2 * m], cfg.shifts[2 * m + 1], td, tw,
                cfg.inverse))
            m += 1
        # leftover last stage (odd n): the trivial W^0 stage as a plain
        # radix-2 SDF stage (D=1). Its reset state aligns directly to the
        # R2² chain's output (verified); it contributes D + NLAYERS = 11.
        self.leftover = None
        if n % 2 == 1:
            self.leftover = _SDFStage(n - 1, N, cfg.shifts[n - 1], td,
                                      [tw[0]], dit=False)
            # the chain's first output arrives at this stage after the
            # R2² latency; the D=1 COMPUTE/PASS phase must be flipped by
            # (chain_latency mod 2) so the first real pair lands on a
            # COMPUTE clock (verified for N = 8..1024)
            for _ in range(sum(st.latency for st in self.stages) % 2):
                self.leftover.step(0, 0)
        self.latency = sum(st.latency for st in self.stages) \
            + (11 if self.leftover is not None else 0)
        # post-warm leftover state (RTL reset preloads): mirrors the plain
        # model's stage_preloads format
        self.leftover_preload = None
        if self.leftover is not None:
            lo = self.leftover
            self.leftover_preload = {
                "wptr": lo.wptr, "pwp": lo.pwp,
                "raddr": (lo.wptr - lo.D) % (2 * lo.D),
                "pipe": list(lo.pipe_comp),
                "phase_i": lo.i, "compute": lo.in_compute,
            }
        self._sb = deque()
        self._cycles = 0

    def reset(self):
        import copy
        for i, st in enumerate(self.stages):
            self.stages[i] = copy.deepcopy(st.__class__(
                st.m, self.N, st.sigma0, st.sigma1, st.td, st.tw,
                st.js == 1))
        self._cycles = 0
        self._sb = deque()

    def tick(self, enabled: bool, re: int = 0, im: int = 0,
             tuser: int = 0, tlast: int = 0):
        """One clock; the R2² stages and the streaming leftover run in
        lockstep."""
        if not enabled:
            return False, 0, 0, 0, 0
        cur = (re, im)
        pos = self._cycles
        up = 0
        for st in self.stages:
            # each stage's schedule is aligned to ITS input stream, which
            # is delayed by the upstream stages' latencies (the plain
            # model's warmup presets do the same alignment)
            cur = st.step(cur, pos - up)
            up += st.latency
        if self.leftover is not None:
            cur = self.leftover.step(cur[0], cur[1])
        self._cycles += 1
        self._sb.append((tuser, tlast))
        valid = self._cycles > self.latency
        if valid:
            u, l = self._sb.popleft()
        else:
            u, l = 0, 0
        return valid, cur[0], cur[1], u, l

    def _apply_leftover(self, frame):
        """Retained for reference only; the streaming leftover in the
        chain replaces the per-frame batch form."""
        return frame

    def process_stream(self, samples, markers=None, frames=None):
        """Run frames through the R2² chain; apply the leftover stage per
        frame; returns one output per input (bit-exact vs the batch
        contract)."""
        N = self.N
        # lockstep the whole stream (plus drain cycles) through the R2²
        # stages and the streaming leftover; drop the warmup so positions
        # 0..T-1 align
        raw = []
        T = len(samples)
        for pos in range(T + self.latency):
            src = samples[pos] if pos < T else (0, 0)
            cur = src[:2]
            up = 0
            for st in self.stages:
                cur = st.step(tuple(cur), pos - up)
                up += st.latency
            if self.leftover is not None:
                cur = self.leftover.step(cur[0], cur[1])
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
# S7/P-next: radix-2^3 contract (merged DIF stage triples, one shared
# complex multiplier per triple + two fabric 45-degree rotates; see
# spikes/S7_r23/ for the schedule derivation and timing probe).
# ----------------------------------------------------------------------

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
# P-next: cycle-accurate streaming R2^3 DIF model
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
        self._sb = deque()
        self._cycles = 0

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

    def tick(self, enabled: bool, re: int = 0, im: int = 0,
             tuser: int = 0, tlast: int = 0):
        """One clock; the R2^3 triples and the streaming leftovers run in
        lockstep (interface parity with R22SDFGoldenModel)."""
        if not enabled:
            return False, 0, 0, 0, 0
        cur = (re, im)
        pos = self._cycles
        up = 0
        for st in self.stages:
            cur = st.step(cur, pos - up)
            up += st.latency
        for st in self.leftovers:
            cur = st.step(cur[0], cur[1])
        self._cycles += 1
        self._sb.append((tuser, tlast))
        valid = self._cycles > self.latency
        if valid:
            u, l = self._sb.popleft()
        else:
            u, l = 0, 0
        return valid, cur[0], cur[1], u, l

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
# P7: cycle-accurate streaming DIT R2² model (mirror topology)
# ----------------------------------------------------------------------

class _R22DITStage:
    """One radix-2² DIT stage (stage pair (A, A+1) merged).

    Mirror of :class:`_R22DIFStage` with multiply-then-combine: the
    three products t1/t2/t3 are computed at the a1/a2/a3 arrivals
    (ONE shared complex multiplier, 75% duty), then the F₄ combine at
    the a3 clock with the exact ±j rotations. Group depth H = 2^A,
    twiddle stride 4^{m'}, block size 4H:

      k in [0, H):    a0 -> v0 = a0 << td -> vline (depth 3H)
      k in [H, 2H):   a1 -> t1 = cmul(a1, T[2j*4^m']) -> t1line (2H)
      k in [2H, 3H):  a2 -> t2 = cmul(a2, T[j*4^m'])   -> t2line (H)
      k in [3H, 4H):  a3 -> t3 = cmul(a3, T[3j*4^m']); F₄ combine
                      (y0 out, y1/y2/y3 into the H/2H/3H queues)

    Position p's value emerges at clock p + 3H (stage latency 3H).
    """

    def __init__(self, A: int, n: int, N: int, sA: int, sB: int,
                 td: int, tw: Sequence[Complex], inverse: bool = False):
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
    def latency(self) -> int:
        return 3 * self.H + 1        # +1: the registered output

    def _rot(self, z: Complex) -> Complex:
        return (-self.js * z[1], self.js * z[0])

    def _prod(self, z: Complex, t: Complex) -> Complex:
        return complex_multiply_karatsuba(z[0], z[1], t[0], t[1])

    def step(self, x: Complex, pos: int) -> Complex:
        H = self.H
        ret = self.out
        r, i = x
        k = pos % (4 * H)
        j = pos % H
        S = self.td + self.sA + self.sB
        cur = (0, 0)
        if k < H:
            self.vline[self.vp] = (r << self.td, i << self.td)
            cur = self.q1[self.q1p]
        elif k < 2 * H:
            self.t1line[self.t1p] = self._prod(
                (r, i), self.tw[(2 * j * self.base) % self.N])
            cur = self.q2[self.q2p]
        elif k < 3 * H:
            self.t2line[self.t2p] = self._prod(
                (r, i), self.tw[(j * self.base) % self.N])
            cur = self.q3[self.q3p]
        else:
            t3 = self._prod((r, i), self.tw[(3 * j * self.base) % self.N])
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


class R22SDFGoldenModelDit:
    """Cycle-accurate streaming model of the R2² DIT pipeline (P7):
    bit-reversed input, natural output (the mirror of the DIF core).

    The odd-n leftover (DIT stage 0, the trivial ±1 stage) runs FIRST
    as a plain radix-2 DIT stage, then the pairs (A, A+1) for
    A = 1, 3, ... (or A = 0, 2, ... for even n). Verified bit-exact
    against :func:`fft_fixed_batch_r22_dit`.
    """

    def __init__(self, cfg: FFTConfig):
        if cfg.ssr != 1:
            raise NotImplementedError("R22 model supports ssr=1 only")
        if not cfg.is_dit or cfg.input_order != "bitreversed" \
                or cfg.output_order != "native":
            raise NotImplementedError(
                "R22 DIT model covers bitreversed->native only")
        self.cfg = cfg
        N = cfg.num_points
        self.N = N
        n = cfg.num_stages
        td = cfg.twiddle_decimal
        tw = canonical_twiddles(N, cfg.twiddle_width, td, cfg.inverse)
        self.leftover = None
        self.stages = []
        lo = 0 if n % 2 == 0 else 1
        if lo == 1:
            self.leftover = _SDFStage(0, N, cfg.shifts[0], td, [tw[0]],
                                      dit=True)
        k = 0
        while lo + 2 * k + 1 < n:
            A = lo + 2 * k
            self.stages.append(_R22DITStage(
                A, n, N, cfg.shifts[A], cfg.shifts[A + 1], td, tw,
                cfg.inverse))
            k += 1
        self.latency = (11 if self.leftover is not None else 0) \
            + sum(st.latency for st in self.stages)

    def process_stream(self, samples, markers=None):
        """Bit-reversed input stream -> natural-order output (bit-exact vs
        the DIT batch contract)."""
        N = self.N
        raw = []
        T = len(samples)
        for pos in range(T + self.latency):
            src = samples[pos] if pos < T else (0, 0)
            cur = src[:2]
            up = 0
            if self.leftover is not None:
                cur = self.leftover.step(cur[0], cur[1])
                up += 11
            for st in self.stages:
                cur = st.step(tuple(cur), pos - up)
                up += st.latency
            raw.append(cur)
        raw = raw[self.latency:]
        outs = []
        for f in range(len(raw) // N):
            frame = raw[f * N:(f + 1) * N]
            q = [quantize_output(re, im, self.cfg.sample_decimal,
                                 self.cfg.output_width,
                                 self.cfg.output_decimal)
                 for re, im in frame]
            if markers is not None:
                for kk, (re, im) in enumerate(q):
                    outs.append((re, im) + tuple(markers[f * N + kk]))
            else:
                outs.extend(q)
        return outs

def fft_fixed_batch_r22_dit(samples: Sequence[Complex], cfg: FFTConfig,
                            twiddles: Optional[Sequence[Complex]] = None
                            ) -> List[Complex]:
    """Fixed-point DIT batch reference, radix-2² contract (P7).

    Mirror of :func:`fft_fixed_batch_r22`: the DIT pair (n−2−2m',
    n−1−2m') merges into one 4-sample group with THREE products applied
    to the inputs BEFORE the F₄ combine (DIT: multiply-then-combine):

        t1 = cmul(a1, T[2j·4^m']);  t2 = cmul(a2, T[j·4^m'])
        t3 = cmul(a3, T[3j·4^m'])                      (exact)
        v0 = a0 << td;   S = td + s_{A} + s_{B}
        y0 = round(v0 + t1 + t2 + t3, S)        # pos j
        y2 = round(v0 + t1 − t2 − t3, S)        # pos j+2H
        y1 = round(v0 − t1 + j·t2 − j·t3, S)    # pos j+H   (j = ±j,
        y3 = round(v0 − t1 − j·t2 + j·t3, S)    # pos j+3H   fwd/inv)

    The ±j rotations are exact (rotation identity T[i+N/4] = ∓j·T[i],
    spikes/S5_r22/). The a3 terms differ from the two-stage DIT (which
    computes (a3·T[2j·4^m'])·T[j·4^m'] — product-of-products) by the
    rounding placement of a3·T[3j·4^m']: the contracts agree to a few
    LSB with identical SQNR. Odd stage counts leave DIT stage 0 (the
    trivial ±1 stage) as plain radix-2.
    """
    if cfg.ssr != 1:
        raise NotImplementedError("batch reference supports ssr=1 only")
    if not cfg.is_dit:
        raise NotImplementedError("batch R2² DIT requires is_dit")
    N = cfg.num_points
    n = cfg.num_stages
    shifts = cfg.shifts
    td = cfg.twiddle_decimal
    if twiddles is None:
        twiddles = canonical_twiddles(N, cfg.twiddle_width,
                                      cfg.twiddle_decimal, cfg.inverse)
    js = -1 if not cfg.inverse else 1     # W^{N/4} = -j fwd, +j inv

    def rot(z):                           # js*j * z
        return (-js * z[1], js * z[0])

    x: List[List[int]] = [[re, im] for re, im in samples]

    # DIT processes stages in input-to-output order: for odd n, stage 0
    # (the trivial ±1 stage) runs first as plain radix-2, then the pairs
    # (lo+2k, lo+2k+1) with lo = 1 (odd n) or 0 (even n).
    lo = 0 if n % 2 == 0 else 1
    for s in range(0, lo):
        half = 1 << s
        step = 2 * half
        sig = shifts[s]
        for start in range(0, N, step):
            for j in range(half):
                i1 = start + j
                i2 = i1 + half
                k = (j << (n - s - 1)) % N
                cr, ci = twiddles[k]
                tr, ti = complex_multiply_karatsuba(x[i2][0], x[i2][1],
                                                    cr, ci)
                sr = (x[i1][0] << td) + tr
                si = (x[i1][1] << td) + ti
                dr = (x[i1][0] << td) - tr
                di = (x[i1][1] << td) - ti
                sh = td + sig
                x[i1] = [round_shift(sr, sh), round_shift(si, sh)]
                x[i2] = [round_shift(dr, sh), round_shift(di, sh)]

    k = 0
    while lo + 2 * k + 1 < n:
        A = lo + 2 * k
        sA, sB = shifts[A], shifts[A + 1]
        H = 1 << A                        # group depth 2^A
        base = 4 ** ((n - 2 - A) // 2)    # twiddle stride 4^{m'}
        for b in range(base):             # base blocks of size 4H
            off = b * (4 * H)
            for j in range(H):
                a0r, a0i = x[off + j]
                a1r, a1i = x[off + j + H]
                a2r, a2i = x[off + j + 2 * H]
                a3r, a3i = x[off + j + 3 * H]
                cr, ci = twiddles[(2 * j * base) % N]
                t1r, t1i = complex_multiply_karatsuba(a1r, a1i, cr, ci)
                cr, ci = twiddles[(j * base) % N]
                t2r, t2i = complex_multiply_karatsuba(a2r, a2i, cr, ci)
                cr, ci = twiddles[(3 * j * base) % N]
                t3r, t3i = complex_multiply_karatsuba(a3r, a3i, cr, ci)
                v0r, v0i = a0r << td, a0i << td
                S = td + sA + sB
                # y0 / y2: real rows of F₄
                x[off + j] = [round_shift(v0r + t1r + t2r + t3r, S),
                              round_shift(v0i + t1i + t2i + t3i, S)]
                x[off + j + 2 * H] = [round_shift(v0r + t1r - t2r - t3r, S),
                                      round_shift(v0i + t1i - t2i - t3i, S)]
                # y1 / y3: ±j rows
                r2r, r2i = rot((t2r, t2i))
                r3r, r3i = rot((t3r, t3i))
                x[off + j + H] = [round_shift(v0r - t1r + r2r - r3r, S),
                                  round_shift(v0i - t1i + r2i - r3i, S)]
                x[off + j + 3 * H] = [round_shift(v0r - t1r - r2r + r3r, S),
                                      round_shift(v0i - t1i - r2i + r3i, S)]
        k += 1

    return [quantize_output(re, im, cfg.sample_decimal,
                            cfg.output_width, cfg.output_decimal)
            for re, im in x]


# ----------------------------------------------------------------------
# L1b: cycle-accurate streaming SDF model
# ----------------------------------------------------------------------

class _SDFStage:
    """One radix-2 DIF stage of the SDF pipeline.

    Alternating phases of ``D`` enabled cycles (period ``N``):
      PASS/FILL : emit stored products from the line, write raw inputs in
      COMPUTE   : pair input ``a`` with delayed ``d``; emit shifted sum,
                  write combined-shifted product into the line

    Reset state is PASS/FILL everywhere; warmup garbage is suppressed by the
    pipeline-level valid window (see SDFGoldenModel.tick).
    """

    # pipeline register layers (mirrors the pipelined RTL stage):
    #   L0 capture (BRAM output register d_bram / DOA_REG)
    #   L1 first  DSP input register  (AREG[0]/DREG[0]/BREG[0])
    #   L2 second DSP input register  (AREG[1]/DREG[1]/BREG[1])
    #   L3 butterfly (pre-adder -> ADREG) + twiddle third hop
    #   L4 products (multiplier -> MREG)
    #   L5 cross products into the C-port registers (CREG)
    #   L6 post-adder (ALU P -/+ C -> PREG)
    #   L7 combine (fabric, aligns sum path with product path)
    #   L8 shift + product-FIFO write
    #   L9 output register
    # The BRAM read-address register (raddr) is modeled explicitly as
    # part of the memory (always wptr - D), matching the RTL's registered
    # address; it adds no data latency (the golden reads at wptr - D, the
    # same cycle the RTL's DOA_REG captures).
    NLAYERS = 10  # register hops from the BRAM address to the output

    def __init__(self, s: int, N: int, sigma: int, td: int,
                 stage_twiddles: Sequence[Complex], dit: bool = False):
        # DIF: depth N/2^(s+1) (deep at the input side);
        # DIT: depth 2^s (mirror -- deep at the output side)
        self.D = (1 << s) if dit else (N >> (s + 1))
        self.dit = dit
        self.sigma = sigma
        self.td = td
        self.T = list(stage_twiddles)          # length D, index by pair i
        # first-half delay RAM (depth 2D, collision-free: read lags write
        # by D) and product FIFO (depth 2D, same lag). The COMPUTE product
        # completes K cycles after its pair and is output D cycles later.
        self.ram = [(0, 0)] * (2 * self.D)
        self.pfifo = [(0, 0)] * (2 * self.D)
        self.wptr = 0
        self.pwp = 0
        self.in_compute = False                # reset state = PASS/FILL
        self.i = 0                             # pair index within phase
        self.raddr = 0                         # BRAM read-address register
        # pipeline registers (10 layers, see NLAYERS docstring)
        self.d_bram = (0, 0)                   # L0: BRAM output register
        self.a_reg = (0, 0)                    # L0: input sample
        self.t_reg = (0, 0)                    # L0: twiddle
        self.d1 = (0, 0)                       # L1: DSP input reg 1 (A/D)
        self.a1 = (0, 0)
        self.t1 = (0, 0)                       # L1: twiddle (B first hop)
        self.d2 = (0, 0)                       # L2: DSP input reg 2 (A/D)
        self.a2 = (0, 0)
        self.t2 = (0, 0)                       # L2: twiddle (B second hop)
        self.bfly_d = (0, 0)                   # L3: pre-adder diff (ADREG)
        self.bfly_s = (0, 0)                   # L3: pre-adder sum (fabric)
        self.t3 = (0, 0)                       # L3: twiddle third hop
        self.prod1 = 0                         # L4/L5: products (MREG)
        self.prod2 = 0
        self.prod3 = 0
        self.prod4 = 0
        self.bfly_h = (0, 0)                   # L4: frozen re-path operands
        self.t3h = (0, 0)
        self.s1 = (0, 0)                       # L4: sum-path delay
        self.c1 = 0                            # L5: C-port regs (CREG)
        self.c2 = 0
        self.s2 = (0, 0)                       # L5: sum-path delay
        self.p = (0, 0)                        # L6: post-adder out (PREG)
        self.s3 = (0, 0)                       # L6: sum-path delay
        self.comb_s = (0, 0)                   # L7: combine (sum path)
        self.comb_p = (0, 0)                   # L7: combine (product path)
        self.shift_s = (0, 0)                  # L8: rounded sum path
        self.shift_p = (0, 0)                  # L8: rounded product path
        self.out_reg = (0, 0)                  # L9: stage output
        self.pipe_comp = [False] * self.NLAYERS  # phase flags riding the pipe
        self._ctor = (s, N, sigma, td, list(stage_twiddles), dit)

    def step(self, ar: int, ai: int) -> Complex:
        """Advance one enabled cycle (10-layer pipelined RTL mirror).

        Register chain (all reads through the pre-edge snapshot S):
          L0 d_bram/a_reg/t_reg   (BRAM output register + input + twiddle)
          L1 d1/a1/t1             (first DSP input register)
          L2 d2/a2/t2             (second DSP input register)
          L3 bfly_d/bfly_s/t3     (pre-adder + ADREG)
          L4 prod1..4, s1         (multiplier -> MREG)
          L5 c1/c2, s2            (cross products -> CREG)
          L6 p, s3                (post-adder P -/+ C -> PREG)
          L7 comb_s/comb_p        (fabric combine)
          L8 shift_s/shift_p      (round + product-FIFO write)
          L9 out_reg              (output register)

        The phase flags ride the pipe (pipe_comp[k] = in_compute(t-k));
        gates follow the validated RTL pattern (layer k gated by the flag
        carried one cycle earlier, see fft_sdf.v).
        """
        result = self.out_reg
        S = dict(self.__dict__)
        w = self.wptr
        # BRAM read-address register: the read lags the write by D. The
        # registered address is (w + 1 - D) updated at the end of the
        # step; at the start of the NEXT step it equals w - D (the read
        # captures the current pointer's lag, exactly like the RTL's
        # raddr_r -> DOA_REG path).
        r = (w + 1 - self.D) % (2 * self.D)
        pr = (self.pwp - self.D) % (2 * self.D)
        cur = self.in_compute
        flags = [cur] + self.pipe_comp[:-1]
        f_bfly, f_mul, f_creg, f_preg, f_comb, f_out = (
            flags[3], flags[4], flags[5], flags[6], flags[7], flags[9])

        sh_sum = self.sigma if not self.dit else self.td + self.sigma
        sh_prod = self.td + self.sigma

        # L9: output register + product-FIFO write-back (COMPUTE writes
        # the rounded product at pwp; PASS reads the product written D
        # cycles earlier at pr = pwp - D). Write and read share the f_out
        # layer so the read/write windows align (validated RTL pattern).
        if f_out:
            self.out_reg = S['shift_s']
            self.pfifo[self.pwp] = S['shift_p']
        else:
            self.out_reg = self.pfifo[pr]

        # L8: round + shift staging (ungated -- always recomputed; the
        # values are only consumed by the f_out layer)
        self.shift_s = (round_shift(S['comb_s'][0], sh_sum),
                        round_shift(S['comb_s'][1], sh_sum))
        self.shift_p = (round_shift(S['comb_p'][0], sh_prod),
                        round_shift(S['comb_p'][1], sh_prod))

        # L7: combine (COMPUTE) or passthrough -- aligns the sum path
        # (delayed butterfly sum / DIT d) with the product path (PREG).
        if f_comb:
            if not self.dit:
                self.comb_s = S['s3']
                self.comb_p = S['p']
            else:
                dr, di = S['s3']          # delayed d (DIT rides d to comb)
                tr, ti = S['p']           # a * W
                self.comb_s = ((dr << self.td) + tr,
                               (di << self.td) + ti)
                self.comb_p = ((dr << self.td) - tr,
                               (di << self.td) - ti)
        else:
            self.comb_s = S['s3']
            self.comb_p = S['s3']

        # L6: post-adder (PREG): re = prod1 - c1, im = prod3 + c2.
        # DSP48E2 C-port pairing: the im-path products (prod2/prod4) run
        # one cycle ahead; their MREGs route to the re-path DSPs' C ports
        # (CREG at L5), so the ALU sees same-pair P (prod1/prod3 computed
        # at L5) and C (prod2/prod4 captured at L5).
        if f_preg:
            self.p = (S['prod1'] - S['c1'],
                      S['prod3'] + S['c2'])
        else:
            self.p = (0, 0)
        self.s3 = S['s2']

        # L5: C-port registers (CREG) + the re-path products (one cycle
        # behind the im-path). The re operands were frozen at L4 into the
        # hold registers (bfly_h/t3h), so the pair matches the CREG value.
        if f_creg:
            self.c1 = S['prod2']
            self.c2 = S['prod4']
            mr, mi = S['bfly_h']
            tr, ti = S['t3h']
            self.prod1 = mr * tr
            self.prod3 = mr * ti
        else:
            self.c1 = 0
            self.c2 = 0
            self.prod1 = 0
            self.prod3 = 0
        self.s2 = S['s1']

        # L4: im-path products (multiplier -> MREG) + freeze of the
        # re-path operands (the re multiply runs one cycle later so the
        # DSP C-port pairing P - C sees the same pair). DIF multiplies
        # the butterfly diff (pre-adder output); DIT multiplies 'a'.
        if f_mul:
            mr, mi = S['bfly_d'] if not self.dit else S['bfly_s']
            tr, ti = S['t3']
            self.prod2 = mi * ti
            self.prod4 = mi * tr
            self.bfly_h = S['bfly_d'] if not self.dit else S['bfly_s']
            self.t3h = S['t3']
        else:
            self.prod2 = 0
            self.prod4 = 0
            self.bfly_h = (0, 0)
            self.t3h = (0, 0)
        # sum path: DIF carries the butterfly sum; DIT carries d
        self.s1 = S['bfly_s'] if not self.dit else S['bfly_d']

        # L3: butterfly (pre-adder -> ADREG) + twiddle third hop
        if f_bfly:
            if not self.dit:
                self.bfly_d = (S['d2'][0] - S['a2'][0],
                               S['d2'][1] - S['a2'][1])
                self.bfly_s = (S['d2'][0] + S['a2'][0],
                               S['d2'][1] + S['a2'][1])
            else:
                self.bfly_d = S['d2']     # d rides to the combine
                self.bfly_s = S['a2']     # a rides to the multiply
        else:
            self.bfly_d = S['d2']
            self.bfly_s = S['d2']
        self.t3 = S['t2']

        # L2: second DSP input register (passthrough)
        self.d2 = S['d1']
        self.a2 = S['a1']
        self.t2 = S['t1']

        # L1: first DSP input register (passthrough)
        self.d1 = S['d_bram']
        self.a1 = S['a_reg']
        self.t1 = S['t_reg']

        # L0: capture -- BRAM output register (registered read), input,
        # twiddle; first-half RAM write (PASS only)
        if not cur:
            self.ram[w] = (ar, ai)
        self.d_bram = self.ram[self.raddr]
        self.a_reg = (ar, ai)
        self.t_reg = self.T[self.i]
        self.pipe_comp = flags

        # FSM advance (both pointers free-run, read lags write by D)
        self.raddr = r
        self.wptr = (w + 1) % (2 * self.D)
        self.pwp = (self.pwp + 1) % (2 * self.D)
        self.i += 1
        if self.i == self.D:
            self.in_compute = not self.in_compute
            self.i = 0
        return result


class SDFGoldenModel:
    """Cycle-accurate model of the R2-SDF DIF pipeline (native -> bitrev).

    tick(enabled, re, im) advances the whole pipeline one clock. Disabled
    cycles freeze every stage (clock-enable semantics, no bubbles enter the
    datapath). Outputs are valid once ``enabled_cycle_count >= latency``.
    """

    def __init__(self, cfg: FFTConfig, dit: bool = False):
        if cfg.ssr != 1:
            raise NotImplementedError("SDFGoldenModel supports ssr=1 only "
                                      "(SSR composition arrives in P4)")
        if dit:
            if cfg.input_order != "bitreversed" or cfg.output_order != "native":
                raise NotImplementedError(
                    "DIT core model covers bitreversed->native only; order "
                    "conversion lives in fft_reorder (P3)")
        else:
            if cfg.input_order != "native" or cfg.output_order != "bitreversed":
                raise NotImplementedError(
                    "DIF core model covers native->bitreversed only; order "
                    "conversion lives in fft_reorder (P3)")
        self.cfg = cfg
        N = cfg.num_points
        self.N = N
        td = cfg.twiddle_decimal
        tw = canonical_twiddles(N, cfg.twiddle_width, td, cfg.inverse)
        self.dit = dit
        n = cfg.num_stages
        self.stages = [
            _SDFStage(
                s, N, cfg.shifts[s], td,
                ([tw[(j << (n - s - 1)) % N] for j in range(1 << s)]
                 if dit else
                 [tw[(i << s) % N] for i in range(N >> (s + 1))]),
                dit=dit)
            for s in range(n)
        ]
        # Schedule latency is N (sum of delay depths) plus the pipeline
        # register layers per stage; verified empirically per config.
        self.latency = N + _SDFStage.NLAYERS * cfg.num_stages
        # FSM presets align every stage's pairing window with its
        # (register-delayed) input stream:
        #   warm_s = -(sum(D_t, t<s) + NLAYERS*s) mod 2*D_s
        # derived exhaustively for small N, verified for large N. In RTL
        # this is a constant counter/phase preload at reset.
        self.stage_presets = []
        self.stage_preloads = []   # full post-warm state for the RTL
        cum = 0
        for s, st in enumerate(self.stages):
            warm = (-(cum + _SDFStage.NLAYERS * s)) % (2 * st.D)
            self.stage_presets.append(warm)
            for _ in range(warm):
                st.step(0, 0)
            cum += st.D
            # RTL reset preload = the exact post-warm state
            self.stage_preloads.append({
                "wptr": st.wptr,
                "pwp": st.pwp,
                "raddr": (st.wptr - st.D) % (2 * st.D),
                "pipe": list(st.pipe_comp),
                "phase_i": st.i,
                "compute": st.in_compute,
            })
        self._cycles = 0          # enabled cycles since reset
        self.frac_out = cfg.sample_decimal
        # frame-marker sideband rides the pipeline: one entry per enabled
        # cycle, popped when the corresponding sample reaches the output
        self._sb = deque()

    def reset(self):
        for st, warm in zip(self.stages, self.stage_presets):
            st.__init__(*st._ctor)             # rebuild fresh state
            for _ in range(warm):
                st.step(0, 0)
        self._cycles = 0
        self._sb = deque()

    def tick(self, enabled: bool, re: int = 0, im: int = 0,
             tuser: int = 0, tlast: int = 0):
        """One clock. If ``enabled`` is false the datapath freezes.

        Returns ``(out_valid, out_re, out_im, out_tuser, out_tlast)``.
        The frame sidebands are transported at the fixed latency: the
        markers fed with input sample i emerge attached to output sample i
        (pinned by tests/test_golden_markers.py).
        """
        if not enabled:
            return False, 0, 0, 0, 0
        cur_re, cur_im = re, im
        for st in self.stages:
            cur_re, cur_im = st.step(cur_re, cur_im)
        self._cycles += 1
        self._sb.append((tuser, tlast))
        valid = self._cycles >= self.latency
        if valid:
            ow, od = self.cfg.output_width, self.cfg.output_decimal
            cur_re, cur_im = quantize_output(cur_re, cur_im,
                                             self.frac_out, ow, od)
            u, l = self._sb.popleft()      # marker of the matching sample
        else:
            u, l = 0, 0                    # warmup: suppressed anyway
        return valid, cur_re, cur_im, u, l

    def process_stream(self, samples: Sequence[Complex],
                       enable: Optional[Sequence[bool]] = None,
                       markers: Optional[Sequence[Tuple[int, int]]] = None
                       ) -> List:
        """Run frames through the model; returns one output per input.

        ``enable`` optionally interleaves disabled cycles between samples
        (same length as ``samples``; True = sample present). ``markers``
        optionally supplies per-sample ``(tuser, tlast)`` pairs; outputs
        are then ``(re, im, out_tuser, out_tlast)`` tuples. Latency flush
        cycles are appended automatically.
        """
        outs: List = []
        for idx, smp in enumerate(samples):
            en = True if enable is None else enable[idx]
            u, l = markers[idx] if markers is not None else (0, 0)
            if en:
                v, re, im, ou, ol = self.tick(True, smp[0], smp[1], u, l)
                if v:
                    outs.append((re, im, ou, ol) if markers is not None
                                else (re, im))
            else:
                self.tick(False)
        # outputs span enabled-ticks [L, L+T-1] for T inputs: the first
        # output emerges on the same tick as input number L, so only L-1
        # trailing enabled cycles are needed to drain.
        for _ in range(self.latency - 1):
            v, re, im, ou, ol = self.tick(True, 0, 0)
            if v:
                outs.append((re, im, ou, ol) if markers is not None
                            else (re, im))
        assert len(outs) == len(samples), \
            f"stream model dropped samples: {len(outs)} != {len(samples)}"
        return outs


# ----------------------------------------------------------------------
# L1a-DIT: fixed-point batch DIT reference (bit-reversed in, natural out)
# ----------------------------------------------------------------------

def fft_fixed_batch_dit(samples, cfg, twiddles=None):
    """Fixed-point DIT batch reference.

    Input is expected in BIT-REVERSED order; output is natural order.
    This is the mirror topology of the DIF core (PLAN.md 2.3): same
    butterflies, twiddle multiplies BEFORE the combine.

    Quantization contract (DIT-specific, pinned for the RTL):
      t_full = x[i2] * W_k            (exact Karatsuba products)
      sum/diff raw = (x[i1] << td) +- t_full     (exact; <<td aligns scales)
      out = round_shift(raw, td + sigma_s)       (ONE rounding point, both
                                                  paths -- symmetric with DIF)
    """
    if cfg.ssr != 1:
        raise NotImplementedError("batch DIT supports ssr=1 only")
    N = cfg.num_points
    n = cfg.num_stages
    shifts = cfg.shifts
    td = cfg.twiddle_decimal
    if twiddles is None:
        twiddles = canonical_twiddles(N, cfg.twiddle_width,
                                      cfg.twiddle_decimal, cfg.inverse)

    x: List[List[int]] = [[re, im] for re, im in samples]

    for s in range(n):
        half = 1 << s
        step = 2 * half
        sig = shifts[s]
        for start in range(0, N, step):
            for j in range(half):
                i1 = start + j
                i2 = i1 + half
                k = (j << (n - s - 1)) % N
                cr, ci = twiddles[k]
                tr, ti = complex_multiply_karatsuba(x[i2][0], x[i2][1],
                                                    cr, ci)
                # scale the delayed (older) value up by 2^td, add exactly,
                # then ONE fused rounding shift
                sr = (x[i1][0] << td) + tr
                si = (x[i1][1] << td) + ti
                dr = (x[i1][0] << td) - tr
                di = (x[i1][1] << td) - ti
                sh = td + sig
                x[i1] = [round_shift(sr, sh), round_shift(si, sh)]
                x[i2] = [round_shift(dr, sh), round_shift(di, sh)]

    return [quantize_output(re, im, cfg.sample_decimal,
                            cfg.output_width, cfg.output_decimal)
            for re, im in x]


# ----------------------------------------------------------------------
# Reorder buffer (ping-pong, N-deep) + full ordering composition
# ----------------------------------------------------------------------

def _bitrev(k: int, n: int) -> int:
    return int(format(k, f"0{n}b")[::-1], 2)


class ReorderModel:
    """Streaming frame reorder: bit-reversal permutation, ping-pong RAM.

    Writes frame f into half-buffer f%2 at natural addresses; reads frame
    f-1 from the other half at bit-reversed addresses. The half offset
    makes read/write addresses structurally disjoint (collision rule,
    PLAN.md 2.7) and keeps the frame boundary clean.

    Latency: N cycles to fill the first frame.
    """

    def __init__(self, N: int):
        self.N = N
        self.n = N.bit_length() - 1
        self.half = N // 2
        self.buf_re = [[0] * N for _ in range(2)]   # 2 halves, N each
        self.buf_im = [[0] * N for _ in range(2)]
        self.buf_extra = [[None] * N for _ in range(2)]  # markers ride along
        self.wpos = 0            # natural write position within frame
        self.frames_written = 0
        self.out_valid = False
        self._cycles = 0

    def reset(self):
        self.wpos = 0
        self.frames_written = 0
        self.out_valid = False
        self._cycles = 0

    def tick(self, enabled: bool, re: int = 0, im: int = 0, extra=None):
        if not enabled:
            return self.out_valid, 0, 0, None
        self._cycles += 1
        # write current sample (extra fields = sidebands, ride along)
        f = self.frames_written % 2
        self.buf_re[f][self.wpos] = re
        self.buf_im[f][self.wpos] = im
        self.buf_extra[f][self.wpos] = extra
        # read previous frame's sample at bitrev position
        valid = self._cycles > self.N
        if valid:
            prev = (self.frames_written - 1) % 2
            p = _bitrev(self.wpos, self.n)
            o_re = self.buf_re[prev][p]
            o_im = self.buf_im[prev][p]
            o_x = self.buf_extra[prev][p]
            self.out_valid = True
        else:
            o_re = o_im = 0
            o_x = None
            self.out_valid = False
        self.wpos += 1
        if self.wpos == self.N:
            self.wpos = 0
            self.frames_written += 1
        return self.out_valid, o_re, o_im, o_x

    def process_stream(self, samples, markers=None):
        outs = []
        for idx, smp in enumerate(samples):
            extra = smp[2:] if len(smp) > 2 else None
            v, re, im, x = self.tick(True, smp[0], smp[1], extra)
            if v:
                outs.append((re, im) if x is None else (re, im) + tuple(x))
        # drain: N cycles flush the last buffered frame (its outputs emerge
        # during the write of the NEXT frame's slots + one cycle)
        for _ in range(self.N):
            v, re, im, x = self.tick(True, 0, 0, None)
            if v:
                outs.append((re, im) if x is None else (re, im) + tuple(x))
        return outs


class OrderedFFTModel:
    """Full streaming FFT model for ANY order corner (PLAN.md 2.3):

      native   -> bitreversed : DIF core
      bitrev   -> native      : DIT core
      native   -> native      : DIF core + output reorder
      bitrev   -> bitrev      : DIT core + output reorder
    """

    def __init__(self, cfg: FFTConfig):
        import copy
        self.cfg = cfg
        N = cfg.num_points
        self.dit = (cfg.input_order == "bitreversed")
        # core is always DIF (native->bitrev) or DIT (bitrev->native);
        # build it with core-natural orders regardless of the outer ones
        core_cfg = copy.copy(cfg)
        core_cfg.input_order = "bitreversed" if self.dit else "native"
        core_cfg.output_order = "native" if self.dit else "bitreversed"
        self.core = SDFGoldenModel(core_cfg, dit=self.dit)
        # extra reorder when the OUTER output order mismatches the core's
        self.reorder_out = (cfg.output_order != core_cfg.output_order)
        self.reorder = ReorderModel(N) if self.reorder_out else None
        self.latency = self.core.latency + (N if self.reorder_out else 0)

    def process_stream(self, samples, markers=None):
        core_out = self.core.process_stream(samples, markers=markers)
        if self.reorder is None:
            return core_out
        if markers is not None:
            # reorder markers along with samples
            outs = []
            for (re, im, u, l) in core_out:
                outs.append((re, im, u, l))
            return self.reorder.process_stream(outs)
        return self.reorder.process_stream(core_out)
