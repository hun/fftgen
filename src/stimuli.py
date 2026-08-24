"""Stimulus generation for golden-model and RTL verification."""

import random
from typing import List, Optional, Sequence, Tuple

Complex = Tuple[int, int]


def random_frame(N: int, width: int, rng: random.Random) -> List[Complex]:
    """Full-scale-ish random complex frame (uniform over signed range)."""
    lo, hi = -(1 << (width - 1)), (1 << (width - 1)) - 1
    return [(rng.randint(lo, hi), rng.randint(lo, hi)) for _ in range(N)]


def impulse(N: int, re: int = 1 << 12, im: int = 0) -> List[Complex]:
    return [(re, im) if n == 0 else (0, 0) for n in range(N)]


def tone(n_index: int, N: int, amplitude: float = 1000.0,
         phase: float = 0.0) -> Complex:
    import math
    ang = 2 * math.pi * n_index / N + phase
    return (int(round(amplitude * math.cos(ang))),
            int(round(amplitude * math.sin(ang))))


def tone_frame(N: int, bin_index: int, amplitude: float = 1000.0,
               phase: float = 0.0) -> List[Complex]:
    return [tone((n * bin_index) % N, N, amplitude, phase)
            for n in range(N)]


def multi_frames(num_frames: int, N: int, width: int,
                 seed: int) -> List[List[Complex]]:
    rng = random.Random(seed)
    return [random_frame(N, width, rng) for _ in range(num_frames)]


def freeze_mask(num_cycles: int, seed: int,
                style: str = "pseudo") -> List[bool]:
    """Enabled mask for a stream of ``num_cycles`` cycles.

    style 'periodic' : every 5th cycle frozen
    style 'pseudo'   : seeded pseudo-random ~30% frozen
    style 'bursty'   : alternating runs of 7 enabled / 3 frozen
    """
    rng = random.Random(seed)
    if style == "periodic":
        return [c % 5 != 4 for c in range(num_cycles)]
    if style == "bursty":
        out = []
        while len(out) < num_cycles:
            out += [True] * 7 + [False] * 3
        return out[:num_cycles]
    if style == "pseudo":
        return [rng.random() > 0.3 for _ in range(num_cycles)]
    raise ValueError(f"unknown freeze style {style!r}")
