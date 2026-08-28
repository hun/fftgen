"""FFT core configuration and validation.

Every constraint from PLAN.md is enforced here so that invalid parameter
combinations are rejected before anything is generated. All widths are
*signed* widths (two's complement).
"""

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

VALID_SSR = (1, 2, 4, 8)
VALID_ORDERS = ("native", "bitreversed")
# "r2"  = plain radix-2 SDF (one butterfly per stage, P0-P6)
# "r22" = radix-2^2 folded SDF (P7): one shared complex multiply per
#         stage pair; re-pins the golden rounding points vs "r2"
#         (1-2 LSB, identical SQNR -- see spikes/S5_r22/notes.md).
#         v1 scope: DIF, native->bitreversed, R=1 (the verified subset).
VALID_STAGE_MODES = ("r2", "r22")


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


@dataclass
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
    output_width: Optional[int] = None    # defaults to sample_width
    output_decimal: Optional[int] = None  # defaults to sample_decimal
    twiddle_width: int = 18               # twiddle ROM word width
    twiddle_decimal: Optional[int] = None # defaults to twiddle_width - 1

    # Per-stage right-shift schedule ("auto" = conservative, cannot overflow;
    # otherwise an explicit list with one entry per stage, entries in 0..2)
    scaling: object = "auto"

    # Stage architecture (see VALID_STAGE_MODES)
    stage_mode: str = "r2"

    def __post_init__(self):
        if self.output_width is None:
            self.output_width = self.sample_width
        if self.output_decimal is None:
            self.output_decimal = self.sample_decimal
        if self.twiddle_decimal is None:
            self.twiddle_decimal = self.twiddle_width - 1

        self._validate()

        if not isinstance(self.scaling, str) or self.scaling != "auto":
            shifts = tuple(self.scaling)
            if len(shifts) != self.num_stages:
                raise ValueError(
                    f"scaling schedule has {len(shifts)} entries, "
                    f"expected num_stages={self.num_stages}")
            for s in shifts:
                if s not in (0, 1, 2):
                    raise ValueError(f"per-stage shift must be 0, 1 or 2, got {s}")
            self.scaling = shifts

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def num_stages(self) -> int:
        return self.num_points.bit_length() - 1  # log2(N)

    @property
    def shifts(self) -> Tuple[int, ...]:
        """Per-stage right-shift schedule (round-half-up at these points)."""
        if isinstance(self.scaling, str) and self.scaling == "auto":
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
            # P7 step 1 verified subset (spikes/S5_r22/rtl_check_prod.py):
            # DIF chain, native -> bitreversed, R = 1. SSR lanes and the
            # DIT / reorder corners open with their own verification
            # steps (P7 step 5 / DIT wiring); until then refuse cleanly.
            if self.ssr != 1:
                raise ValueError(
                    f"stage_mode='r22' supports ssr=1 only for now "
                    f"(SSR r22 arrives with P7 step 5), got ssr={self.ssr}")
            if self.input_order != "native" or self.output_order != "bitreversed":
                raise ValueError(
                    "stage_mode='r22' supports native -> bitreversed only "
                    f"for now, got {self.input_order} -> {self.output_order}")
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
            if width is not None and width < 2:
                raise ValueError(f"{name} must be >= 2, got {width}")
        for name, width, dec in (
                ("sample", self.sample_width, self.sample_decimal),
                ("output", self.output_width, self.output_decimal),
                ("twiddle", self.twiddle_width, self.twiddle_decimal)):
            if dec is not None and not (0 <= dec < width):
                raise ValueError(
                    f"{name}_decimal must satisfy 0 <= decimal < width, "
                    f"got width={width}, decimal={dec}")
