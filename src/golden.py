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
