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

from typing import List, Sequence, Tuple
import math

from config import FFTConfig
from golden import SDFGoldenModel, ReorderModel
from twiddles import canonical_twiddles
from quant import round_shift, quantize_output


class _SSRLane:
    """One R=1 engine: DIF core + ping-pong reorder -> native order."""

    def __init__(self, m_cfg: FFTConfig):
        import copy
        core_cfg = copy.copy(m_cfg)
        core_cfg.input_order = "native"
        core_cfg.output_order = "bitreversed"
        self.core = SDFGoldenModel(core_cfg, dit=False)
        self.reorder = ReorderModel(m_cfg.num_points)
        self.latency = self.core.latency + m_cfg.num_points

    def tick(self, enabled: bool, re: int, im: int, u: int, l: int):
        v1, re, im, uu, ll = self.core.tick(enabled, re, im, u, l)
        v2, re, im, extra = self.reorder.tick(enabled and v1, re, im,
                                              (uu, ll))
        if extra is None:
            return v2, re, im, 0, 0
        return v2, re, im, extra[0], extra[1]


class SSRGoldenModel:
    """Streaming SSR FFT model. See module docstring for the contract."""

    def __init__(self, cfg: FFTConfig):
        if cfg.ssr < 2:
            raise ValueError("SSRGoldenModel requires cfg.ssr >= 2")
        if cfg.input_order != "native":
            raise NotImplementedError("SSR v1 supports native input only")
        if cfg.output_order != "native":
            raise NotImplementedError("SSR v1 supports native output only")
        R = cfg.ssr
        M = cfg.num_points // R
        self.cfg = cfg
        self.R = R
        self.M = M
        import copy
        lane_cfg = copy.copy(cfg)
        lane_cfg.num_points = M
        lane_cfg.ssr = 1
        self.lanes = [_SSRLane(lane_cfg) for _ in range(R)]
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
        for ln in self.lanes:
            ln.core.reset()
            ln.reorder.reset()
        self._cycles = 0
        self._slot = 0

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
        p = (self._cycles - self.lanes[0].latency) % M
        self._dbg_p = p
        self._dbg_lane = [(lo[1], lo[2], lo[0]) for lo in lane_out]
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


def ssr_emission_to_native(num_points: int, ssr: int) -> List[int]:
    """perm[e] = natural sample index carried by emission slot e."""
    M = num_points // ssr
    return [(e % ssr) * M + (e // ssr) for e in range(num_points)]
