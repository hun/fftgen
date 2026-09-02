#!/usr/bin/env python3
"""Round-trip validation for the r23 DIT stage (S8, bitrev->natural IFFT).

_R23DIFStage(sigmas=0, tw_inv, js=+1) followed by
_R23DITStage(sigmas=0, tw_fwd, js=-1, shift_extra=3) must recover the
DIF stage's input stream: the DIT is the exact transpose of the DIF,
and the DIF's output stream (sub-transforms in bitrev member order)
is exactly the DIT's expected input format -- no reorder in between.

The forward-kernel DIT (js=-1) inverts the inverse-kernel DIF; the
shift_extra=3 absorbs the DIF's synthesis factor 8 (all sigmas zero).
Passing here validates the entire streaming schedule (arrival windows,
depth-lag lines, combine alignment, natural-order emission queues,
latencies 7G+2 / 7G+1) bit-exact up to product/rot rounding.
"""
import random
import sys

sys.path.insert(0, "src")
from golden import _R23DIFStage, _R23DITStage
from twiddles import canonical_twiddles

TD = 17                      # the project convention: decimal = width - 1
WIDTH = 18                   # (Q2.17 in 18 bits, unsaturated unit twiddles)


def run_roundtrip(N, m, blocks=3, seed=1234, verbose=True):
    G = N >> (3 * m + 3)
    assert G >= 1
    tw_inv = canonical_twiddles(N, WIDTH, TD, True)
    tw_fwd = canonical_twiddles(N, WIDTH, TD, False)
    dif = _R23DIFStage(m, N, 0, 0, 0, TD, tw_inv, inverse=True)
    dit = _R23DITStage(m, N, 0, 0, 0, TD, tw_fwd, inverse=False,
                       shift_extra=3)

    rng = random.Random(seed + N + m)
    T = blocks * 8 * G
    xin = [(rng.randint(-8, 8), rng.randint(-8, 8)) for _ in range(T)]

    # ---- DIF pass: x[p] at clock p, collect returns ----
    dout = []
    for p in range(T):
        dout.append(dif.step(xin[p], p))
    for _ in range(dif.latency):
        dout.append(dif.step((0, 0), T + len(dout)))

    # ---- DIT pass: the member at DIT clock pos = the DIF's output
    #      position pos = the DIF's return from clock pos + L1 ----
    L1 = dif.latency
    L2 = dit.latency
    maxd = 0
    checked = 0
    first_bad = None
    for q in range(L1, L1 + T):
        pos = q - L1
        ret = dit.step(dout[q], pos)
        p = pos - L2
        if p >= 0:
            exp = xin[p]
            d = max(abs(ret[0] - exp[0]), abs(ret[1] - exp[1]))
            if d > maxd:
                maxd = d
            if d > 4 and first_bad is None:
                first_bad = (p, ret, exp)
            checked += 1

    ok = maxd <= 4 and checked > 0
    if verbose:
        print(f"N={N:6d} m={m} G={G:5d} checked={checked:6d} "
              f"max|delta|={maxd:2d} {'PASS' if ok else 'FAIL'}")
        if first_bad:
            p, ret, exp = first_bad
            print(f"   first bad: p={p} got={ret} exp={exp}")
    return ok, maxd


def main():
    cases = [
        (512, 0), (1024, 0), (4096, 0),
        (4096, 1), (8192, 1), (32768, 2),
        (65536, 3), (65536, 4),
    ]
    all_ok = True
    for N, m in cases:
        ok, _ = run_roundtrip(N, m)
        all_ok &= ok
    print("ALL PASS" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
