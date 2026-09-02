#!/usr/bin/env python3
"""Float validation of the r23 DIT chain (S8, bitrev -> natural IFFT).

R23SDFGoldenModelDit.process_stream on a random bit-reversed spectrum
must reproduce the float IDFT (x[n] = sum_k X[k] e^{+2 pi i k n / N} / N)
within a few LSB (twiddle quantization + per-stage rounding).
"""
import cmath
import random
import sys

sys.path.insert(0, "src")
from config import FFTConfig
from golden import R23SDFGoldenModelDit


def float_idft(X, N):
    return [sum(X[k] * cmath.exp(2j * cmath.pi * k * n / N)
                for k in range(N)) / N for n in range(N)]


def bitrev(p, bits):
    v = 0
    for _ in range(bits):
        v = (v << 1) | (p & 1)
        p >>= 1
    return v


def run(N, seed, verbose=True):
    cfg = FFTConfig(num_points=N, input_order="bitreversed",
                    output_order="native", inverse=True)
    assert sum(cfg.shifts) == N.bit_length() - 1
    rng = random.Random(seed + N)
    bits = N.bit_length() - 1
    # spectrum in natural order, quantized to the input format, then fed
    # in bitrev order (samples[p] = X[bitrev(p)])
    Xnat = [(rng.randint(-900, 900), rng.randint(-900, 900))
            for _ in range(N)]
    stream = [(Xnat[bitrev(p, bits)][0], Xnat[bitrev(p, bits)][1])
              for p in range(N)]
    model = R23SDFGoldenModelDit(cfg)
    outs = model.process_stream(stream * 2)   # 2 frames: warmup + measured
    frame = outs[N:2 * N]                     # second frame (steady state)
    xf = float_idft([complex(re, im) for re, im in Xnat], N)
    worst = 0
    worst_n = None
    for n in range(N):
        got = complex(frame[n][0], frame[n][1])
        d = max(abs(got.real - xf[n].real), abs(got.imag - xf[n].imag))
        if d > worst:
            worst, worst_n = d, n
    ok = worst <= 4
    if verbose:
        print(f"N={N:6d} worst|delta|={worst:7.3f} at n={worst_n} "
              f"{'PASS' if ok else 'FAIL'}")
        if not ok:
            n = worst_n
            print(f"   got={frame[n]}  float={xf[n]:.3f}")
    return ok


def main():
    all_ok = True
    for N in (512, 1024, 2048, 4096, 8192):
        all_ok &= run(N, seed=42)
    print("ALL PASS" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
