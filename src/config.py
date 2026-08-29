"""FFT core configuration and validation.

Every constraint from PLAN.md is enforced here so that invalid parameter
combinations are rejected before anything is generated. All widths are
*signed* widths (two's complement).
"""

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple, Union

VALID_SSR = (1, 2, 4, 8)
VALID_ORDERS = ("native", "bitreversed")
# "r2"  = plain radix-2 SDF (one butterfly per stage, P0-P6)
# "r22" = radix-2^2 folded SDF (P7): one shared complex multiply per
#         stage pair; re-pins the golden rounding points vs "r2"
#         (1-2 LSB, identical SQNR -- see spikes/S5_r22/notes.md).
#         v1 scope: DIF, native->bitreversed, R=1 (the verified subset).
VALID_STAGE_MODES = ("r2", "r22")

# P8 -- SSR corner orders (doc/plan_p8_ssr_orders.md). The SSR crossbar
# emits X[qM+p] block-contiguous, i.e. native, by construction; a bitrev_N
# emission additionally requires bitrev_R on the lane axis:
#
#     bitrev_N(R*c + q) = bitrev_M(c)*R + bitrev_R(q)
#
# bitrev_2 is the IDENTITY, so at R=2 the lane axis needs no permutation and
# the corner order collapses onto the M axis inside each lane (DIF lanes with
# their reorder off + a bit-reversed crossbar bin index). bitrev_4=(0 2 1 3)
# and bitrev_8 are NOT affine mod R, hence not absorbable into DFT wiring:
# R>2 needs a real R-wide bitrev_N buffer and stays out of this subset.
# Tuple key: (stage_mode, ssr, input_order, output_order, inverse).
SSR_CORNER_ORDERS = {
    ("r22", 2, "native", "bitreversed", False),   # FFT nat->bitrev (P8 step 1)
    ("r22", 2, "bitreversed", "native", True),    # IFFT bitrev->nat (P8 step 4a),
    #                                   reuse-verified-blocks route (crossbar-
    #                                   first + per-lane input reorder); the
    #                                   BOTH flat emission conventions: slot e
    #                                   carries index bitrev_N(e) in and x[e] out
}


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


@dataclass(init=False)
class FFTConfig:
    """Complete FFT/IFFT core configuration (see PLAN.md section 1)."""

    num_points: int                       # N, power of two, >= 2
    inverse: bool = False                 # False = FFT, True = IFFT
    ssr: int = 1                          # samples per clock, in VALID_SSR

    # Stream ordering (DIF/DIT topology chosen accordingly, see PLAN.md 2.3)
    input_order: str = "native"           # "native" | "bitreversed"
    output_order: str = "bitreversed"     # "native" | "bitreversed"

    # Fixed-point widths (all signed/two's complement)
    sample_width: int = 16                # input I/Q width
    sample_decimal: int = 0               # input fractional bits
    output_width: int = 16                # defaults to sample_width
    output_decimal: int = 0               # defaults to sample_decimal
    twiddle_width: int = 18               # twiddle ROM word width
    twiddle_decimal: int = 17             # defaults to twiddle_width - 1

    # Per-stage right-shift schedule ("auto" = conservative, cannot overflow;
    # otherwise an explicit list with one entry per stage, entries in 0..2)
    scaling: Union[str, Tuple[int, ...]] = "auto"

    # Stage architecture (see VALID_STAGE_MODES)
    stage_mode: str = "r2"

    def __init__(self, num_points: int, inverse: bool = False, ssr: int = 1,
                 input_order: str = "native",
                 output_order: str = "bitreversed",
                 sample_width: int = 16, sample_decimal: int = 0,
                 output_width: Optional[int] = None,
                 output_decimal: Optional[int] = None,
                 twiddle_width: int = 18,
                 twiddle_decimal: Optional[int] = None,
                 scaling: Union[str, Sequence[int]] = "auto",
                 stage_mode: str = "r2"):
        """Normalized fields; Optional args default to pipeline-consistent
        values (output_width -> sample_width, output_decimal ->
        sample_decimal, twiddle_decimal -> twiddle_width - 1)."""
        self.num_points = num_points
        self.inverse = inverse
        self.ssr = ssr
        self.input_order = input_order
        self.output_order = output_order
        self.sample_width = sample_width
        self.sample_decimal = sample_decimal
        self.output_width = (sample_width if output_width is None
                             else output_width)
        self.output_decimal = (sample_decimal if output_decimal is None
                               else output_decimal)
        self.twiddle_width = twiddle_width
        self.twiddle_decimal = (twiddle_width - 1 if twiddle_decimal is None
                                else twiddle_decimal)
        self.scaling = (scaling if isinstance(scaling, str)
                        else tuple(scaling))
        self.stage_mode = stage_mode

        self._validate()

        if isinstance(self.scaling, str):
            if self.scaling != "auto":
                raise ValueError(
                    "scaling schedule must be 'auto' or an explicit "
                    f"per-stage shift list, got {self.scaling!r}")
        else:
            if len(self.scaling) != self.num_stages:
                raise ValueError(
                    f"scaling schedule has {len(self.scaling)} entries, "
                    f"expected num_stages={self.num_stages}")
            for s in self.scaling:
                if s not in (0, 1, 2):
                    raise ValueError(
                        f"per-stage shift must be 0, 1 or 2, got {s}")

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def num_stages(self) -> int:
        return self.num_points.bit_length() - 1  # log2(N)

    @property
    def shifts(self) -> Tuple[int, ...]:
        """Per-stage right-shift schedule (round-half-up at these points)."""
        if isinstance(self.scaling, str):   # "auto" (the only legal str)
            if self.num_points == 1:
                return (0,)
            # Conservative: every radix-2 butterfly at most doubles the
            # component magnitude (twiddle components are <= 1 in magnitude),
            # so one bit per stage keeps |value| <= max|x| forever. Proven
            # bound, see PLAN.md 2.5 and tests/test_scaling.py.
            return (1,) * self.num_stages
        return tuple(self.scaling)

    @property
    def scaling_guaranteed(self) -> bool:
        """True if the schedule provably cannot overflow (sum >= log2(N))."""
        return sum(self.shifts) >= self.num_stages

    @property
    def is_dit(self) -> bool:
        """True if the emitted topology is DIT (bit-reversed input)."""
        return self.input_order == "bitreversed"

    @property
    def is_r22(self) -> bool:
        """True if the core uses the radix-2^2 folded datapath (P7)."""
        return self.stage_mode == "r22"

    def ssr_corner_supported(self) -> bool:
        """True if (ssr, orders, inverse) is inside the P8 verified SSR
        corner-order subset (config.SSR_CORNER_ORDERS)."""
        return (self.stage_mode, self.ssr, self.input_order,
                self.output_order, self.inverse) in SSR_CORNER_ORDERS

    def __repr__(self):
        return (f"FFTConfig(N={self.num_points}, inverse={self.inverse}, "
                f"ssr={self.ssr}, in={self.input_order}, out={self.output_order}, "
                f"mode={self.stage_mode}, "
                f"W={self.sample_width}.{self.sample_decimal}->"
                f"{self.output_width}.{self.output_decimal}, "
                f"tw={self.twiddle_width}.{self.twiddle_decimal}, "
                f"scaling={'auto' if isinstance(self.scaling, str) else list(self.shifts)})")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self):
        if self.stage_mode not in VALID_STAGE_MODES:
            raise ValueError(
                f"stage_mode must be one of {VALID_STAGE_MODES}, "
                f"got {self.stage_mode!r}")
        if self.stage_mode == "r22":
            # P7 verified subsets: DIF chain only. R=1 covers native ->
            # bitreversed (steps 1-4); R>1 reuses that exact lane core
            # behind the unchanged crossbar (step 5) and shares the SSR
            # v1 native -> native contract, except for the P8 corner-order
            # subset below (SSR_CORNER_ORDERS). The subset tuple fully
            # determines (input_order, output_order, inverse), so a config
            # is either in it or it must be all-native.
            if self.input_order != "native" \
                    and not self.ssr_corner_supported():
                raise ValueError(
                    "stage_mode='r22' is DIF-only: input_order must be "
                    f"'native' (or the P8 corner subset "
                    f"{sorted(SSR_CORNER_ORDERS)}), got {self.input_order!r}")
            if self.ssr == 1 and self.output_order != "bitreversed":
                raise ValueError(
                    "stage_mode='r22' (R=1) supports native -> bitreversed "
                    f"only for now, got output_order={self.output_order!r}")
            if self.ssr > 1 and self.output_order != "native" \
                    and not self.ssr_corner_supported():
                raise ValueError(
                    f"stage_mode='r22' with ssr={self.ssr} shares the SSR "
                    "contract: output_order must be 'native', got "
                    f"{self.output_order!r} (supported corner-order subset: "
                    f"{sorted(SSR_CORNER_ORDERS)})")
        elif self.ssr > 1 and (self.input_order != "native"
                               or self.output_order != "native") \
                and not self.ssr_corner_supported():
            # stage_mode='r2' used to accept SSR corner orders here and then
            # die on an assert deep inside fft_gen.generate_ssr.
            raise ValueError(
                f"stage_mode='r2' with ssr={self.ssr} shares the SSR "
                "contract: input_order/output_order must be 'native', got "
                f"{self.input_order!r}/{self.output_order!r} (supported "
                f"corner-order subset: {SSR_CORNER_ORDERS})")
        if not is_power_of_two(self.num_points) or self.num_points < 2:
            raise ValueError(
                f"num_points must be a power of two >= 2, got {self.num_points}")
        if self.ssr not in VALID_SSR:
            raise ValueError(
                f"ssr must be one of {VALID_SSR}, got {self.ssr}")
        if self.num_points % self.ssr != 0:
            raise ValueError(
                f"ssr={self.ssr} must divide num_points={self.num_points}")
        for name, order in (("input_order", self.input_order),
                            ("output_order", self.output_order)):
            if order not in VALID_ORDERS:
                raise ValueError(
                    f"{name} must be one of {VALID_ORDERS}, got {order!r}")
        for name, width in (("sample_width", self.sample_width),
                            ("output_width", self.output_width),
                            ("twiddle_width", self.twiddle_width)):
            if width < 2:
                raise ValueError(f"{name} must be >= 2, got {width}")
        for name, width, dec in (
                ("sample", self.sample_width, self.sample_decimal),
                ("output", self.output_width, self.output_decimal),
                ("twiddle", self.twiddle_width, self.twiddle_decimal)):
            if not (0 <= dec < width):
                raise ValueError(
                    f"{name}_decimal must satisfy 0 <= decimal < width, "
                    f"got width={width}, decimal={dec}")
