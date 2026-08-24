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

    def __init__(self, s: int, N: int, sigma: int, td: int,
                 stage_twiddles: Sequence[Complex]):
        self.D = N >> (s + 1)
        self.sigma = sigma
        self.td = td
        self.T = list(stage_twiddles)          # length D, index by pair i
        self.buf = deque([(0, 0)] * self.D)         # the delay line: D entries
        self.in_compute = False                # reset state = PASS/FILL
        self.i = 0                             # pair index within phase

    def step(self, ar: int, ai: int) -> Complex:
        """Advance one enabled cycle; returns this cycle's output."""
        if self.in_compute:
            d_re, d_im = self.buf.popleft()
            sum_re = round_shift(ar + d_re, self.sigma)
            sum_im = round_shift(ai + d_im, self.sigma)
            # diff contract = OLDER - NEWER (matches batch reference
            # x[i1] - x[i2]); the delayed sample d is the older one.
            dr, di = d_re - ar, d_im - ai
            cr, ci = self.T[self.i]
            pr, pi = complex_multiply_karatsuba(dr, di, cr, ci)
            sh = self.td + self.sigma          # combined normalization+scaling
            prod = (round_shift(pr, sh), round_shift(pi, sh))
            # write product; the line drains raw during COMPUTE and refills
            # with products.
            self.buf.append(prod)
            self.i += 1
            if self.i == self.D:
                self.in_compute = False
                self.i = 0
            return sum_re, sum_im
        else:
            out = self.buf.popleft()           # stored product (or garbage)
            self.buf.append((ar, ai))          # fill raw
            self.i += 1
            if self.i == self.D:
                self.in_compute = True
                self.i = 0
            return out


class SDFGoldenModel:
    """Cycle-accurate model of the R2-SDF DIF pipeline (native -> bitrev).

    tick(enabled, re, im) advances the whole pipeline one clock. Disabled
    cycles freeze every stage (clock-enable semantics, no bubbles enter the
    datapath). Outputs are valid once ``enabled_cycle_count >= latency``.
    """

    def __init__(self, cfg: FFTConfig):
        if cfg.ssr != 1:
            raise NotImplementedError("SDFGoldenModel supports ssr=1 only "
                                      "(SSR composition arrives in P4)")
        if cfg.input_order != "native" or cfg.output_order != "bitreversed":
            raise NotImplementedError(
                "core model covers native->bitreversed only; order "
                "conversion lives in fft_reorder (P3)")
        self.cfg = cfg
        N = cfg.num_points
        self.N = N
        td = cfg.twiddle_decimal
        tw = canonical_twiddles(N, cfg.twiddle_width, td, cfg.inverse)
        self.stages = [
            _SDFStage(s, N, cfg.shifts[s], td,
                      [tw[(i << s) % N] for i in range(N >> (s + 1))])
            for s in range(cfg.num_stages)
        ]
        self.latency = N      # derived + verified: see appendix A / tests
        self._cycles = 0          # enabled cycles since reset
        self.frac_out = cfg.sample_decimal
        # frame-marker sideband rides the pipeline: one entry per enabled
        # cycle, popped when the corresponding sample reaches the output
        self._sb = deque()

    def reset(self):
        for st in self.stages:
            st.buf.clear()
            st.buf.extend([((0, 0))] * st.D)
            st.in_compute = False
            st.i = 0
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
