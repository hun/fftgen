"""SSR golden model (PLAN.md 2.4): Cooley-Tukey N = R * M composition.

Architecture (mirrors the planned RTL exactly):

  input word (R consecutive samples x[R*j .. R*j+R-1]) at clock j
    -> lane r gets x[R*j+r]  (stride-R demux, free wiring)
    -> lane r: M-point R=1 SDF engine (DIF core + output reorder),
       emitting A_r[p] in natural p order -- one value per clock,
       all lanes aligned
    -> crossbar: B_r = A_r * W_N^{r p}   (pre-twiddle)
                 X[Mq+p] = sum_r W_R^{r q} B_r   (R-point DFT over lanes)
    -> output word at clock p carries X[Mq+p] on lane q ("block
       contiguous": over a frame, lane q holds the contiguous block
       X[qM .. qM+M-1])

P8 -- output_order="bitreversed" (R == 2, arch r22; FFT native -> bitrev):

  bitrev_N(R*c + q) = bitrev_M(c)*R + bitrev_R(q), and bitrev_2 is the
  IDENTITY, so a frame emitted with slot e carrying X[bitrev_N(e)] needs
  only (a) the lane reorder DISABLED (the DIF lane already emits
  bitrev_M(c) at clock c) and (b) the crossbar's bin index taken as
  bitrev_M(counter) for the WN row lookup. No lane permutation, no
  reorder memory anywhere -- and lane latency DROPS by M (the contract
  change this mode advertises). 0 and M-1 are bitrev fixed points, so the
  frame markers keep their exact clocks (SOF on the first emission, EOF
  on the last). Everything downstream of the p index is order-agnostic.

Fixed-point contract for the crossbar:
  * lane outputs are already quantized to output_width (they are the
    engines' final outputs)
  * pre-twiddle products are kept EXACT (no truncation)
  * the R-point accumulation is EXACT
  * one fused rounding shift s_x = log2(R) (conservative worst-case
    growth bound of the R-point sum)
  * final quantize_output back to output_width

Twiddles come from canonical_twiddles() (single source of truth);
inverse configurations conjugate both tables identically.

Output markers ride through the lanes: SOF enters with sample n=0
(lane 0) and emerges on output lane 0 at p=0; EOF enters with n=N-1
(lane R-1) and emerges on lane R-1 at p=M-1.
"""

from collections import deque
from typing import List, Optional, Sequence, Tuple
import math

from config import FFTConfig, SSR_CORNER_ORDERS
from golden import SDFGoldenModel, R22SDFGoldenModel, R23ChainGoldenModel, ReorderModel, _bitrev
from twiddles import canonical_twiddles
from quant import round_shift, quantize_output


class _SSRLane:
    """One R=1 engine: DIF core + ping-pong reorder -> native order.

    arch = "r2" (plain radix-2, ``fft_sdf``) or "r22" (radix-2^2,
    ``fft_sdf_r22`` / ``R22SDFGoldenModel``). The r22 RTL pipeline is
    deeper than the 3*D+1 golden stage (the verified pipelined stage
    emits position p at clock p + 3D + 9, plus the wrapper's registered
    quantizer output), so an extra uniform delay of (valid, data,
    markers) aligns the model lane to the RTL:
    extra = 8*npairs + 1.
    """

    def __init__(self, m_cfg: FFTConfig, arch: str = "r2",
                 reorder_out: bool = True):
        import copy
        from collections import deque
        core_cfg = copy.copy(m_cfg)
        core_cfg.input_order = "native"
        core_cfg.output_order = "bitreversed"
        # extra delay FIFO + depth: only used by the r22 arch (deeper RTL
        # pipeline than the golden stage); None for plain radix-2
        self._extra_q: Optional[deque] = None
        self._extra = 0
        if arch == "r23":
            self.core = R23ChainGoldenModel(core_cfg)
            # the chain model is already RTL-cycle-aligned (its delta
            # delay folds the RTL pipeline depth; valid starts at
            # transform(0) like the RTL's tuser-checked mvalid)
            self._extra = 0
            self._extra_q = None
        elif arch == "r22":
            self.core = R22SDFGoldenModel(core_cfg)
            # RTL r22 core latency (verified, rtl/fft_sdf_r22.v):
            #   sum(3*D + 9) per pair + 11 (odd-n leftover, D + NLAYERS)
            #   + 1 (registered quantizer output)
            n = core_cfg.num_stages
            M = m_cfg.num_points
            rtl_core_lat = sum(3 * (M >> (2 * m + 2)) + 9
                               for m in range(n // 2)) \
                + (11 if n % 2 else 0) + 1
            # golden core latency (3*D+1 per pair + 11 leftover)
            self._extra = rtl_core_lat - self.core.latency
            self._extra_q = deque()
            # prefill extra delay with invalid entries
            for _ in range(max(0, self._extra)):
                self._extra_q.append((False, 0, 0, 0, 0))
        else:
            self.core = SDFGoldenModel(core_cfg, dit=False)
        self.arch = arch
        self.reorder_out = reorder_out
        self.reorder = ReorderModel(m_cfg.num_points) if reorder_out else None
        # lane latency as seen by SSR crossbar (core + optional reorder + extra)
        ring = m_cfg.num_points if reorder_out else 0
        if arch == "r22":
            self.latency = self.core.latency + ring + max(0, self._extra)
        elif arch == "r23":
            self.latency = self.core.latency
        else:
            self.latency = self.core.latency + ring

    def tick(self, enabled: bool, re: int, im: int, u: int, l: int):
        v1, re, im, uu, ll = self.core.tick(enabled, re, im, u, l)
        if self.arch == "r23":
            return v1, re, im, uu, ll
        if self.arch == "r22" and self._extra > 0:
            # delay core valid+data to match RTL pipeline (3D+9 vs 3D+1)
            assert self._extra_q is not None
            self._extra_q.append((v1, re, im, uu, ll))
            v1, re, im, uu, ll = self._extra_q.popleft()
        if self.reorder is None:
            # P8: the DIF lane's own output order is bitrev -- exactly the
            # order the bitrev_N emission needs at R == 2, so no buffer.
            return v1, re, im, uu, ll
        v2, re, im, extra = self.reorder.tick(enabled and v1, re, im,
                                              (uu, ll))
        if extra is None:
            return v2, re, im, 0, 0
        return v2, re, im, extra[0], extra[1]


class SSRGoldenModel:
    """Streaming SSR FFT model. See module docstring for the contract.

    arch selects the per-lane engine: "r2" (default, plain radix-2)
    or "r22" (P7, one multiply/pair). Both share the same crossbar.
    """

    def __init__(self, cfg: FFTConfig, arch: str = "r2"):
        if cfg.ssr < 2:
            raise ValueError("SSRGoldenModel requires cfg.ssr >= 2")
        if cfg.input_order != "native" or cfg.output_order != "native":
            # P8 verified subset (see SSR_CORNER_ORDERS); anything else still
            # needs bitrev_R to be a real permutation -> not generatable.
            if not cfg.ssr_corner_supported():
                raise NotImplementedError(
                    f"SSR supports native -> native only, plus the P8 subset "
                    f"{sorted(SSR_CORNER_ORDERS)}; got ssr={cfg.ssr} "
                    f"{cfg.input_order} -> {cfg.output_order} "
                    f"inverse={cfg.inverse} arch={arch!r}")
        if arch not in ("r2", "r22", "r23"):
            raise ValueError(f"arch must be 'r2' or 'r22', got {arch!r}")
        R = cfg.ssr
        M = cfg.num_points // R
        self.cfg = cfg
        self.R = R
        self.M = M
        self.arch = arch
        # P8: bitrev emission == DIF lanes with their reorder off, and the
        # crossbar walking its bin index in bitrev_M order (see module
        # docstring). Native output keeps the historical configuration.
        self.emit_brev = cfg.output_order == "bitreversed"
        self.lm = M.bit_length() - 1
        import copy
        lane_cfg = copy.copy(cfg)
        lane_cfg.num_points = M
        lane_cfg.ssr = 1
        self.lanes = [_SSRLane(lane_cfg, arch=arch,
                              reorder_out=not self.emit_brev)
                      for _ in range(R)]
        if R >= 8:
            self.CB_LAT = 11    # +input reg, G/H, Q-reg, partials, scalar
        self.latency = self.lanes[0].latency + self.CB_LAT

        # crossbar twiddle tables (quantized, single source of truth)
        td = cfg.twiddle_decimal
        full = canonical_twiddles(cfg.num_points, cfg.twiddle_width, td,
                                  cfg.inverse)
        small = canonical_twiddles(R, cfg.twiddle_width, td, cfg.inverse)
        # WN[r][p] = W_N^{r p}; WR[r][q] = W_R^{r q}
        self.wn = [[full[(r * p) % cfg.num_points] for p in range(M)]
                   for r in range(R)]
        self.wr = [[small[(r * q) % R] for q in range(R)]
                   for r in range(R)]
        self.s_x = R.bit_length() - 1        # log2(R) conservative shift

        self._cycles = 0                     # enabled words consumed
        self._synced = False                 # waiting for p == 0

    CB_LAT = 7                              # input+fetch+mult+combine+DFT
                                            # +halfshift+rescale

    # ------------------------------------------------------------------
    def reset(self):
        from collections import deque
        for ln in self.lanes:
            ln.core.reset()
            if ln.reorder is not None:
                ln.reorder.reset()
            if ln.arch == "r23":
                ln._extra_q = None
            elif ln.arch == "r22" and ln._extra > 0:
                ln._extra_q = deque()
                for _ in range(ln._extra):
                    ln._extra_q.append((False, 0, 0, 0, 0))
        self._cycles = 0
        self._slot = 0
        self._synced = False

    def tick(self, word_re: Sequence[int], word_im: Sequence[int],
             mk: Sequence[Tuple[int, int]]):
        """Advance one enabled clock with an R-sample word.

        Returns (valid, outs, mks) where outs/mks are R-tuples holding
        the crossbar outputs for this clock (garbage when not valid).
        Output starts on the first p == 0 slot after the pipeline has
        filled, so the emission stream is always frame aligned.
        """
        R, M = self.R, self.M
        # lanes: stride-R demux
        lane_out = []
        for r in range(R):
            lane_out.append(self.lanes[r].tick(True, word_re[r],
                                               word_im[r],
                                               mk[r][0], mk[r][1]))
        self._cycles += 1
        # the lanes carry A_r[p] with p counted from THEIR first valid
        # output (they all share the same latency, hence lockstep)
        # lanes: the r22 model lane carries the RTL's extra pipeline
        # depth in _extra; the crossbar phase convention (p counts from
        # the first lane-valid word) then lags the data by one word
        p_off = 1 if self.arch in ("r22", "r23") else 0
        p_seq = (self._cycles - self.lanes[0].latency - p_off) % M
        # P8: with the lane reorders off the lanes present A_r[p] with p
        # already in bitrev_M order, so the SEQUENTIAL counter must be
        # bit-reversed to name the actual bin the WN row belongs to.
        p = _bitrev(p_seq, self.lm) if self.emit_brev else p_seq
        self._dbg_p = p
        self._dbg_lane = [(lo[1], lo[2], lo[0]) for lo in lane_out]
        # Frame sync: emit from the first p==0 word after the pipeline
        # is filled, dropping the fill frames. The RTL's fft_cross syncs
        # to the same word (mature = scnt > CB_LAT+1; p_off here handles
        # the r22 extra-depth lag in the lane convention). A single-frame
        # stream never reaches a second p==0 slot and produces NO output
        # -- callers must supply >= 2 frames (generate_ssr prepends
        # pad_frames fillers).
        filled = self._cycles > self.lanes[0].latency + self.CB_LAT
        if not self._synced:
            if filled and p == 0:
                self._synced = True
            else:
                return False, [(0, 0)] * R, [(0, 0)] * R
        valid = self._synced

        outs, mks = [], []
        if valid:
            ow = self.cfg.output_width
            od = self.cfg.output_decimal
            # Lane-DFT coefficient contract:
            #   R <= 4: every W_R^{rq} is exactly {0,+/-1,+/-j}; the
            #       combining network is add/sub/swap only.
            #   R >= 8: W_R entries are {+/-1,+/-j} or
            #       (+/-sqrt(2)/2)(+/-1 +/- j). Group each output into
            #       E_q (trivial-coefficient terms, exact adds at
            #       od + td fractional bits) and F_q ((+/-1 +/- j)
            #       terms, also exact adds), then apply ONE Q(td)
            #       real scalar c = round(sqrt(2)/2 * 2^td):
            #           C_q = E_q + round_shift(F_q * c, td)
            #       Products return to od + td bits, so the final
            #       quantize shifts by td only (NOT 2*td).
            exact_wr = R <= 4
            frac = od + self.cfg.twiddle_decimal  # both modes end at od+td
            c8 = None if exact_wr else \
                round((2 ** self.cfg.twiddle_decimal) * (2 ** -0.5))
            inv = self.cfg.inverse
            sgn = -1 if inv else 1
            for q in range(R):
                er = ei = fr = fi = 0
                for r in range(R):
                    vr, vi = lane_out[r][1], lane_out[r][2]
                    wr_, wi_ = self.wn[r][p]
                    tr = vr * wr_ - vi * wi_
                    ti = vr * wi_ + vi * wr_
                    m = (r * q) % R
                    if exact_wr:
                        # exact W_R^{rq}: angle = -sgn*2*pi*rq/R
                        if m == 0:
                            cr, ci = 1, 0
                        elif m * 4 == R:
                            cr, ci = 0, (-sgn)
                        elif m * 2 == R:
                            cr, ci = -1, 0
                        else:  # m*4 == 3R
                            cr, ci = 0, sgn
                        er += tr * cr - ti * ci
                        ei += tr * ci + ti * cr
                    else:
                        ang = -sgn * 2 * math.pi * m / R
                        cr = math.cos(ang)
                        ci = math.sin(ang)
                        if abs(cr) < 0.5 or abs(ci) < 0.5:
                            # trivial {0, +/-1} + j{0, +/-1}
                            er += tr * round(cr) - ti * round(ci)
                            ei += tr * round(ci) + ti * round(cr)
                        else:                   # sqrt(2)/2 * (+/-1 +/- j)
                            sc = round(cr)
                            ss = round(ci)
                            fr += tr * sc - ti * ss
                            fi += ti * sc + tr * ss
                if exact_wr:
                    sr = er
                    si = ei
                else:
                    assert c8 is not None
                    sr = er + round_shift(fr * c8,
                                          self.cfg.twiddle_decimal)
                    si = ei + round_shift(fi * c8,
                                          self.cfg.twiddle_decimal)
                sr = round_shift(sr, self.s_x)
                si = round_shift(si, self.s_x)
                outs.append(quantize_output(sr, si, frac, ow, od))
                mks.append((lane_out[q][3], lane_out[q][4]))
        else:
            outs = [(0, 0)] * R
            mks = [(0, 0)] * R
        return valid, outs, mks

    # ------------------------------------------------------------------
    def process_stream(self, samples: Sequence[Tuple[int, int]],
                       markers=None) -> List[Tuple[int, int, int, int]]:
        """Feed flat natural-order samples (R per clock); returns the FLAT
        emission stream: for each clock, the R lane outputs in lane order
        (emission index e corresponds to natural sample
        n = (e % R)*M + e//R)."""
        R, M = self.R, self.M
        assert len(samples) % (R * M) == 0
        if markers is None:
            markers = [(0, 0)] * len(samples)
        outs = []
        n_words = len(samples) // R
        # drain: keep clocking until the last real sample has emerged
        drain = ((self.latency + R - 1) // R) + 2
        for j in range(n_words + drain):
            if j < n_words:
                word = samples[j * R:(j + 1) * R]
                mk = markers[j * R:(j + 1) * R]
            else:
                word = [(0, 0)] * R
                mk = [(0, 0)] * R
            valid, owords, omks = self.tick(
                [w[0] for w in word], [w[1] for w in word], mk)
            if valid:
                for q in range(R):
                    re, im = owords[q]
                    u, l = omks[q]
                    outs.append((re, im, u, l))
        return outs


class SSRCornerInverseModel:
    """P8 (R = 2, r22): SSR IFFT, corner order bitreversed -> native.

    Contract (the FLAT corner conventions, mirroring R=1 DIT and the P8
    forward FFT):

        slot e = (2c+q) IN  carries the bin  X[bitrev_N(e)] = X[qM + bitrev_M(c)]
        slot e = (2c+q) OUT carries the time sample x[e] = x[2c+q]

    Concatenating the P8 forward FFT (slot e emits X[bitrev_N(e)]) with
    this IFFT is therefore the identity on x -- exactly the property a
    fast-convolution TX/RX chain relies on.

    Transpose structure: the R-point step runs FIRST, the per-lane engines
    LAST. For R = 2 the R-point inverse is add/sub, and the input word
    already carries both q values at the same p = bitrev_M(c), so per clock:

        a0 = round_shift(x0 + x1, 1)               -> lane 0 (twiddle W^0 = 1)
        a1 = round_shift(x0 - x1, 1) * conj(W_N^p) -> lane 1

    Each lane input is then reordered bitrev_M -> native (the same ping-pong
    as R=1's fft_reorder) and fed to the EXISTING verified M-point DIF-IDFT
    engine (REORDER_OUT = 1, native-in native-out). Lane r emits
    (1/M) sum_p a_r[p] w_M^{-jp} at native j, i.e. x[2c+q] -- flat native.

    Rounding (mirrors the forward crossbar): the add/sub is exact (one
    guard bit), the fused >>1 (s_x = log2 R) is round-half-up, the
    post-twiddle product is kept EXACT, and the wrapper quantizes exactly
    once to sample_width (the lane-input contract). a0 needs no requantise:
    the halved sum of two in-range samples fits sample_width exactly.
    """

    def __init__(self, cfg: FFTConfig, arch: str = "r22"):
        import copy
        if arch != "r22" or cfg.ssr != 2 or not cfg.inverse:
            raise ValueError("SSRCornerInverseModel is R=2/r22/IFFT only")
        if not cfg.ssr_corner_supported():
            raise NotImplementedError(f"not in the P8 corner subset: {cfg}")
        N = cfg.num_points
        M = N // 2
        self.cfg = cfg
        self.M = M
        self.lm = M.bit_length() - 1
        lane_cfg = copy.copy(cfg)
        lane_cfg.num_points = M
        lane_cfg.ssr = 1
        # existing verified r22 lanes: DIF-IDFT core + output reorder,
        # i.e. M-point native-in native-out inverse engines
        self.lanes = [_SSRLane(lane_cfg, arch="r22") for _ in range(2)]
        # per-lane input reorders: bitrev-M arrival -> native order
        self.reorders = [ReorderModel(M) for _ in range(2)]
        # conjugated twiddle row r = 1: W_N^{-p} (lane 0 needs no multiply)
        full = canonical_twiddles(N, cfg.twiddle_width,
                                  cfg.twiddle_decimal, inverse=True)
        self.w1 = [full[p] for p in range(M)]
        # wrapper pipeline depth: add/sub+twiddle -> quantize register
        self.WRAP = 2
        self._conv = self.WRAP + M + self.lanes[0].latency
        self.latency = self._conv
        self._cycles = 0
        self._synced = False

    def reset(self):
        from collections import deque
        for ln in self.lanes:
            ln.core.reset()
            if ln.reorder is not None:
                ln.reorder.reset()
            if ln.arch == "r23":
                ln._extra_q = None
            elif ln.arch == "r22" and ln._extra > 0:
                ln._extra_q = deque()
                for _ in range(ln._extra):
                    ln._extra_q.append((False, 0, 0, 0, 0))
        for ro in self.reorders:
            ro.reset()
        self._cycles = 0
        self._synced = False

    def tick(self, word_re: Sequence[int], word_im: Sequence[int],
             mk: Sequence[Tuple[int, int]]):
        M = self.M
        p = _bitrev(self._cycles % M, self.lm)
        self._cycles += 1
        # R-point inverse step + fused >>1 (s_x = log2 R = 1), round-half-up
        a0r = round_shift(word_re[0] + word_re[1], 1)
        a0i = round_shift(word_im[0] + word_im[1], 1)
        a1r = round_shift(word_re[0] - word_re[1], 1)
        a1i = round_shift(word_im[0] - word_im[1], 1)
        sw, sd = self.cfg.sample_width, self.cfg.sample_decimal
        wr, wi = self.w1[p]
        # exact product, then ONE quantize to sample_width (lane input)
        t1r = a1r * wr - a1i * wi
        t1i = a1r * wi + a1i * wr
        a1r, a1i = quantize_output(t1r, t1i,
                                   sd + self.cfg.twiddle_decimal, sw, sd)
        outs, mks = [], []
        for r in range(2):
            a_r = (a0r, a0i) if r == 0 else (a1r, a1i)
            vr, cr, ci, ex = self.reorders[r].tick(
                True, a_r[0], a_r[1], mk[r])
            u, l = (0, 0) if ex is None else ex
            v2, or_, oi, ou, ol = self.lanes[r].tick(vr, cr, ci, u, l)
            if not v2:
                outs.append((0, 0)); mks.append((0, 0))
            else:
                outs.append((or_, oi)); mks.append((ou, ol))
        if not self._synced:
            if self._cycles > self._conv:
                self._synced = True
            else:
                return False, [(0, 0)] * 2, [(0, 0)] * 2
        return True, outs, mks

    def process_stream(self, samples: Sequence[Tuple[int, int]],
                       markers=None) -> List[Tuple[int, int, int, int]]:
        R, M = 2, self.M
        assert len(samples) % (R * M) == 0
        if markers is None:
            markers = [(0, 0)] * len(samples)
        outs = []
        n_words = len(samples) // R
        drain = ((self.latency + R - 1) // R) + 2
        for j in range(n_words + drain):
            if j < n_words:
                word = samples[j * R:(j + 1) * R]
                mk = markers[j * R:(j + 1) * R]
            else:
                word = [(0, 0)] * R
                mk = [(0, 0)] * R
            valid, owords, omks = self.tick(
                [w[0] for w in word], [w[1] for w in word], mk)
            if valid:
                for q in range(R):
                    outs.append((owords[q][0], owords[q][1],
                                 omks[q][0], omks[q][1]))
        return outs


def ssr_emission_to_native(num_points: int, ssr: int) -> List[int]:
    """perm[e] = natural sample index carried by emission slot e."""
    M = num_points // ssr
    return [(e % ssr) * M + (e // ssr) for e in range(num_points)]


def ssr_emission_perm(num_points: int, ssr: int,
                      output_order: str = "native") -> List[int]:
    """perm[e] = natural index carried by flat emission slot e, for either
    output order. `native` is the block-contiguous SSR contract; `bitreversed`
    is the P8 corner order (slot e carries X[bitrev_N(e)] -- the same
    convention fft_reorder imposes at R = 1).

    At R = 2 the two views agree with the implementation identity
    bitrev_N(R*c + q) = bitrev_M(c)*R + q, which is what makes the corner
    order free of any lane permutation.
    """
    if output_order == "native":
        return ssr_emission_to_native(num_points, ssr)
    if output_order != "bitreversed":
        raise ValueError(f"bad output_order {output_order!r}")
    n = num_points.bit_length() - 1
    return [_bitrev(e, n) for e in range(num_points)]
