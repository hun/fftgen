"""Twiddle factor generation and quantization.

The canonical twiddle table is defined here once; RTL ROMs (full or
quarter-wave compressed, decision PLAN.md 7.6) must reproduce it exactly.

Definitions
-----------
``T[k]`` approximates ``W_N^k = exp(-2*pi*i*k/N)`` (forward) resp.
``exp(+2*pi*i*k/N)`` (inverse), built **magnitude-first** so every
symmetry is an exact sign flip:

    A[k] = sat(round_half_up(2**dec * |cos(2*pi*k/N)|))
    B[k] = sat(round_half_up(2**dec * |sin(2*pi*k/N)|))
    forward:  T[k] = (sgn(cos)*A[k], -sgn(sin)*B[k])
    inverse:  same with + on the imaginary part

``sat`` clamps to the signed ``twiddle_width``-bit range. Because Q1.(w-1)
cannot represent +1.0, unit-magnitude endpoints saturate to
``2**decimal - 1`` -- a deliberate, documented sub-unity bias (component
magnitudes stay <= 1, which the datapath overflow-proofness argument relies
on). Magnitude-first construction means negation NEVER leaves the format
(no rail asymmetry between ``-sat(+x)`` and ``sat(-x)``).

Quarter-wave compression (PLAN.md 7.6): only ``k in [0, N/4]`` is stored;
all other indices are reconstructed by exact index/sign symmetries, so
``decode_quarter_wave(...)`` equals the canonical entry bit-exactly
(pinned by exhaustive tests over all k for many N).
"""

import math
from typing import List, Tuple


def _round_half_up_real(x: float) -> int:
    """Deterministic real-domain round-half-up (floor(x + 0.5))."""
    return math.floor(x + 0.5)


def _mag_table(N: int, width: int, decimal: int, which: str) -> List[int]:
    """A (cos) or B (sin) magnitude table for k = 0 .. N/4 inclusive."""
    scale = 1 << decimal
    hi = (1 << (width - 1)) - 1
    lo = -(1 << (width - 1))

    def sat(v: int) -> int:
        return lo if v < lo else hi if v > hi else v

    out = []
    for k in range(N // 4 + 1):
        ang = 2.0 * math.pi * k / N
        v = abs(math.cos(ang) if which == "cos" else math.sin(ang))
        out.append(sat(_round_half_up_real(scale * v)))
    return out


def quarter_wave_rom(N: int, width: int,
                     decimal: int) -> Tuple[List[int], List[int]]:
    """Stored ROM contents ``(A, B)``: |cos| / |sin| for k = 0 .. N/4.

    Requires ``N >= 4`` (the DIF pipeline's first stage never multiplies,
    so no real ROM exists below that anyway).
    """
    if N < 4:
        raise ValueError("quarter_wave_rom requires N >= 4")
    return (_mag_table(N, width, decimal, "cos"),
            _mag_table(N, width, decimal, "sin"))


def decode_quarter_wave(k: int, N: int, A: List[int], B: List[int],
                        inverse: bool = False) -> Tuple[int, int]:
    """Reconstruct T[k mod N] from the quarter-wave ROMs, bit-exactly.

    All corrections are pure index mirrors and sign flips, hence exact.
    """
    k %= N
    q = N // 4
    if k <= q:
        re, im = A[k], -B[k]
    elif k < 2 * q:
        r = k - q                       # (pi/2, pi):   cos=-sin, sin=+cos
        re, im = -B[r], -A[r]
    elif k < 3 * q:
        r = k - 2 * q                   # [pi, 3pi/2):  cos=-cos, sin=-sin
        re, im = -A[r], B[r]
    else:
        r = N - k                       # [3pi/2, 2pi): cos=+cos, sin=+sin
        re, im = A[r], B[r]
    if inverse:
        im = -im
    return re, im


def canonical_twiddles(N: int, width: int, decimal: int,
                       inverse: bool = False) -> List[Tuple[int, int]]:
    """Full canonical table, k = 0 .. N-1.

    For N >= 4 this is defined as the quarter-wave decode of the stored ROMs
    (single source of truth). N = 2 degenerates to [(1-ish, 0), (-1-ish, 0)].
    """
    if N < 4:
        hi = (1 << (width - 1)) - 1
        return [(hi, 0), (-hi, 0)]
    A, B = quarter_wave_rom(N, width, decimal)
    return [decode_quarter_wave(k, N, A, B, inverse) for k in range(N)]
