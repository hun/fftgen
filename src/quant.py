"""Canonical fixed-point quantization — the single source of truth.

Every quantization point of the datapath is defined here and nowhere else;
both golden models and any analysis tooling must use these exact functions so
the numerical contract has one definition (PLAN.md 2.5).

Conventions
-----------
* All values are plain Python ints carrying an implicit two's-complement
  interpretation; fractional bits are tracked by the caller.
* ``round_shift(v, s)`` implements the *round-half-up* arithmetic right
  shift used at scaling points and product normalization:
  ``(v + 2^(s-1)) >> s`` for ``s > 0``, identity for ``s == 0``
  (matches RTL ``(sum + (1 <<< (s-1))) >>>> s``).
* Quantization points of the FFT datapath:

  1. butterfly sum        ``a + b``                       exact
  2. butterfly difference ``a - b``                       exact
  3. complex product      full-precision integer products exact
  4. multiply path        ``round_shift(prod, twiddle_decimal + shift_s)``
     (one combined shift normalizes the twiddle Q-format AND applies the
     stage scaling in a single rounding operation -- this is what the RTL
     does after the DSP48 P port)
  5. sum path             ``round_shift(a + b, shift_s)``
  6. output               ``quantize_output``: round-shift to the output
     fractional format, then saturate to output_width

* Products are never truncated separately (point 3 is exact); the only
  lossy operations are the shifts in 4/5 and the final output quantization.
"""

from typing import Tuple


def round_shift(v: int, s: int) -> int:
    """Arithmetic right shift by ``s`` with round-half-up."""
    if s <= 0:
        return v << (-s)          # negative shifts are exact left shifts
    return (v + (1 << (s - 1))) >> s


def saturate(v: int, width: int) -> int:
    """Clamp to signed ``width``-bit range."""
    lo = -(1 << (width - 1))
    hi = (1 << (width - 1)) - 1
    return lo if v < lo else hi if v > hi else v


def round_shift_sat(v: int, s: int, width: int) -> int:
    """Round-half-up shift followed by saturation (used by callers needing
    both in one step)."""
    return saturate(round_shift(v, s), width)


def complex_multiply_karatsuba(ar: int, ai: int,
                               cr: int, ci: int) -> Tuple[int, int]:
    """Exact complex product via the 3-multiplier Karatsuba form that the
    RTL uses on DSP48 pre-adders:

        m1 = c*a, m2 = d*b, m3 = (a+b)*(c+d)
        re = m1 - m2
        im = m3 - m1 - m2          (== a*d + b*c, verified)

    Kept here so the golden model exercises exactly the identity the
    hardware will compute (all three products are exact big-ints, so the
    identity holds bit-exactly; only the operand widths differ from RTL).
    """
    m1 = cr * ar
    m2 = ci * ai
    m3 = (ar + ai) * (cr + ci)
    return m1 - m2, m3 - m1 - m2


def quantize_output(re: int, im: int, frac_from: int,
                    out_width: int, out_decimal: int) -> Tuple[int, int]:
    """Final output quantization: rescale from ``frac_from`` fractional bits
    to ``out_decimal`` (round-half-up when shrinking, exact left shift when
    growing), then saturate to ``out_width``."""
    s = frac_from - out_decimal
    re = round_shift(re, s) if s != 0 else re
    im = round_shift(im, s) if s != 0 else im
    return saturate(re, out_width), saturate(im, out_width)


def wrap(v: int, width: int) -> int:
    """Two's-complement wrap to ``width`` bits (used only by optional
    narrow-word experiments and by tests exercising overflow behaviour)."""
    m = v & ((1 << width) - 1)
    if m >= (1 << (width - 1)):
        m -= 1 << width
    return m
