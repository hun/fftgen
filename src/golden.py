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

    # pipeline register layers (mirrors the pipelined RTL stage):
    #   R2 read-data regs  -> R3 butterfly  -> R4 multiply (MREG)
    #   -> R5 combine -> R6 shift+out
    NLAYERS = 6   # register hops from R2 capture to R7 output
                  # (R3 DSP operand regs map onto AREG/BREG/DREG, R4 diff
                  # onto pre-adder ADREG, R5 products onto MREG, R6 combine
                  # onto the C-port ALU + PREG)

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
        # pipeline registers
        self.d_reg = (0, 0)                    # R2: memory read data
        self.a_reg = (0, 0)                    # R2: input sample
        self.tw_reg = (0, 0)                   # R2: twiddle
        self.x_reg = (0, 0)                    # R3: d side operand (DSP D/A)
        self.y_reg = (0, 0)                    # R3: a side operand (DSP B)
        self.tw_a = (0, 0)                     # R3: twiddle first hop
        self.tw_b = (0, 0)                     # R4: twiddle second hop
        self.bfly_sum = (0, 0)                 # R4: butterfly sum path
        self.bfly_diff = (0, 0)                # R4: butterfly diff (=ADREG)
        self.mreg = (0, 0)                     # R4: multiply output
        self.comb_sum = (0, 0)                 # R5: combine (sum path)
        self.comb_prod = (0, 0)                # R5: combine (product path)
        self.out_reg = (0, 0)                  # R6: stage output
        self.pipe_comp = [False] * self.NLAYERS  # phase flags riding the pipe
        self.sum_dly = (0, 0)                  # R4: sum-path delay register
        self.a_dly = (0, 0)                    # R4: a reaches the multiply (DIT)
        self.d_dly = (0, 0)                    # R3: d first hop
        self.d_dly2 = (0, 0)                   # R4: d reaches the combine (DIT)
        self._ctor = (s, N, sigma, td, list(stage_twiddles), dit)

    def step(self, ar: int, ai: int) -> Complex:
        """Advance one enabled cycle (pipelined RTL mirror).

        FIFO depth D+K, write at w, read at w-D. The twiddle and the
        sum/a/d operands travel delay registers alongside the datapath so
        the multiply (R4) and combine (R5) see their pair's values.
        """
        result = self.out_reg
        w = self.wptr
        r = (w - self.D) % (2 * self.D)
        pr = (self.pwp - self.D) % (2 * self.D)
        d_val = self.ram[r]
        cur = self.in_compute
        flags = [cur] + self.pipe_comp[:-1]
        f_r7, f_r6c, f_r5m, f_r4b = flags[5], flags[4], flags[3], flags[2]

        # R7: shift + out; product write-back to the product FIFO
        sh_sum = self.sigma if not self.dit else self.td + self.sigma
        sh_prod = self.td + self.sigma
        if f_r7:
            out_val = (round_shift(self.comb_sum[0], sh_sum),
                       round_shift(self.comb_sum[1], sh_sum))
            prod_val = (round_shift(self.comb_prod[0], sh_prod),
                        round_shift(self.comb_prod[1], sh_prod))
            self.pfifo[self.pwp] = prod_val
        else:
            out_val = self.pfifo[pr]           # product output (D later)
        self.out_reg = out_val

        # R6: combine (COMPUTE) or passthrough -- C-port ALU + PREG
        if f_r6c:
            if not self.dit:
                self.comb_prod = self.mreg
                self.comb_sum = self.sum_dly
            else:
                tr, ti = self.mreg
                self.comb_sum = ((self.d_dly2[0] << self.td) + tr,
                                 (self.d_dly2[1] << self.td) + ti)
                self.comb_prod = ((self.d_dly2[0] << self.td) - tr,
                                  (self.d_dly2[1] << self.td) - ti)
        else:
            self.comb_prod = self.d_dly2
            self.comb_sum = self.d_dly2

        # R5: multiply (COMPUTE) or passthrough; delay regs advance.
        # The products land in MREG; the twiddle arrives via its second
        # delay hop so it meets the ADREG output here.
        if f_r5m:
            m = ((self.bfly_diff, self.tw_b) if not self.dit
                 else (self.a_dly, self.tw_b))
            m1 = (m[0][0] * m[1][0], m[0][1] * m[1][1])
            m3 = ((m[0][0] + m[0][1]) * (m[1][0] + m[1][1]),)
            self.mreg = (m1[0] - m1[1], m3[0] - m1[0] - m1[1])
        else:
            self.mreg = self.d_dly2
        self.sum_dly = self.bfly_sum
        self.d_dly2 = self.d_dly

        # R4: butterfly from the DSP operand registers -- maps onto the
        # pre-adder (diff) with its output register (ADREG); the sum is
        # computed alongside in fabric
        if f_r4b:
            if not self.dit:
                self.bfly_diff = (self.x_reg[0] - self.y_reg[0],
                                  self.x_reg[1] - self.y_reg[1])
                self.bfly_sum = (self.x_reg[0] + self.y_reg[0],
                                 self.x_reg[1] + self.y_reg[1])
            else:
                self.bfly_diff = self.x_reg
                self.bfly_sum = self.y_reg
        else:
            self.bfly_diff = self.x_reg
            self.bfly_sum = self.x_reg
        self.tw_b = self.tw_a                  # twiddle second hop
        self.d_dly = self.bfly_diff
        if self.dit:
            self.a_dly = self.bfly_sum         # a rides to the multiply

        # R3: DSP operand registers (ungated passthrough; candidates for
        # AREG/BREG/DREG absorption by inference)
        self.x_reg = self.d_reg
        self.y_reg = self.a_reg
        self.tw_a = self.tw_reg                # twiddle first hop

        # R2: first-half RAM write (PASS only) + read capture
        if not cur:
            self.ram[w] = (ar, ai)             # PASS: first half, read-first
        self.d_reg = d_val
        self.a_reg = (ar, ai)
        self.tw_reg = self.T[self.i]
        self.pipe_comp = flags

        # FSM advance (both pointers free-run, read lags write by D)
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
        self.wpos = 0            # natural write position within frame
        self.frames_written = 0
        self.out_valid = False
        self._cycles = 0

    def reset(self):
        self.wpos = 0
        self.frames_written = 0
        self.out_valid = False
        self._cycles = 0

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
