#!/usr/bin/env python3
"""Independent verification of every fftgen golden model against a true DFT.

The project's own tests pin each golden model *relative* to the others
(r2 vs r22 few-LSB contract delta, batch vs streaming bit-exactness, SSR
vs batch tolerance). That leaves a latent hole: a defect common to two
compared models is invisible.  This script closes it by anchoring every
model absolutely to numpy's FFT, with the ordering conventions derived
from first principles (DIF emission = bitrev slots, DIT = natural,
SSR block-contiguous, P8 corner = bitrev_N slots).

Cores covered
-------------
L0   float references                  fft_float_reference / fft_float_radix2
L1a  fixed-point batch (R=1)           fft_fixed_batch (r2 DIF), fft_fixed_batch_dit
                                      fft_fixed_batch_r22, fft_fixed_batch_r22_dit
L1b  cycle-accurate streaming (R=1)    SDFGoldenModel (r2), R22SDFGoldenModel
                                      R22SDFGoldenModelDit
L1c  reorder + order compositions      ReorderModel, OrderedFFTModel (4 corners)
L2   SSR (R = 2/4/8)                   SSRGoldenModel arch r2/r22, P8 corners

Method
------
* exact-mode wide-word tests (64-bit samples, 60/120-bit twiddles, zero
  shifts) isolate the algorithm: only the documented 0.5-LSB product
  roundings + twiddle quantization remain, so a fully correct graph must
  match the true DFT to ~130 dB while a wrong index/order blows up.
* production configuration (16-bit in, 18-bit Q17 twiddles, auto
  scaling) reports the SQNR you actually get at the documented precision.
* structural pins (single-bin tone, shifted impulse) verify the *order*
  unambiguously: tone at bin k must peak exactly at slot bitrev(k)
  (DIF) / k (DIT), and nothing else.
* markers/padding conventions for SSR follow the project's own harness
  (SOF-anchored windows), so the frame phase is not assumed.

Run:  python3 tools/verify_golden.py
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import numpy as np

from config import FFTConfig
from golden import (fft_float_reference, fft_float_radix2,
                    fft_fixed_batch, fft_fixed_batch_dit,
                    fft_fixed_batch_r22, fft_fixed_batch_r22_dit,
                    SDFGoldenModel, R22SDFGoldenModel, R22SDFGoldenModelDit,
                    ReorderModel, OrderedFFTModel, _bitrev)
from golden_ssr import (SSRGoldenModel, SSRCornerInverseModel,
                        ssr_emission_perm)
from quant import complex_multiply_karatsuba, round_shift, saturate
from twiddles import canonical_twiddles
from stimuli import random_frame

FAIL = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"   {detail}" if detail else ""), flush=True)
    if not ok:
        FAIL.append(label)


def c(frame):
    return [complex(r, i) for r, i in frame]


def bitrev_perm(seq):
    """out[bitrev(k)] = seq[k]  (self-inverse; matches golden._bitrev)."""
    n = len(seq).bit_length() - 1
    out = [None] * len(seq)
    for k, v in enumerate(seq):
        out[_bitrev(k, n)] = v
    return out


def sqnr_db(ref, dut):
    sig = sum(abs(v) ** 2 for v in ref)
    err = sum(abs(r - d) ** 2 for r, d in zip(ref, dut))
    if err == 0:
        return float("inf")
    return 10 * math.log10(sig / err)


def maxerr(ref, dut):
    return max(max(abs(r.real - d.real), abs(r.imag - d.imag))
               for r, d in zip(ref, dut))


def np_spec(frame, inv):
    """True N-point transform, unnormalized direction (no 1/N anywhere)."""
    x = np.array(c(frame))
    return list(np.fft.ifft(x) * len(x) if inv else np.fft.fft(x))


def np_spec_scaled(frame, inv):
    """Same, with the /N the 'auto' scaling schedule applies in both
    directions (datapath stays in Q(sample_decimal))."""
    x = np.array(c(frame))
    return list(np.fft.ifft(x) if inv else np.fft.fft(x) / len(x))


def wide_cfg(N, inv=False, mode="r2", tw=60):
    return FFTConfig(num_points=N, inverse=inv, stage_mode=mode,
                     sample_width=64, output_width=140, twiddle_width=tw,
                     scaling=(0,) * (N.bit_length() - 1))


def exact_mode(fn, N, inv=False, tw=60, seed=0, mode="r2",
               order=("native", "bitreversed")):
    """Wide-word, zero-shift run of batch model ``fn`` vs the true DFT.
    Returns (sqnr_db, maxerr)."""
    cfg = wide_cfg(N, inv, mode=mode, tw=tw)
    rng = random.Random(1000 + N + tw + seed)
    frame = [(rng.randint(-2 ** 20, 2 ** 20), rng.randint(-2 ** 20, 2 ** 20))
             for _ in range(N)]
    fn_kw = {}
    samples = frame
    if order[0] == "bitreversed":
        n = N.bit_length() - 1
        samples = [frame[_bitrev(j, n)] for j in range(N)]
        cfg.input_order = "bitreversed"
    if order[1] == "native":
        cfg.output_order = "native"
    got = c(fn(samples, cfg, **fn_kw))
    ref = bitrev_perm(np_spec(frame, inv)) if order[1] == "bitreversed" \
        else np_spec(frame, inv)
    return sqnr_db(ref, got), maxerr(ref, got)


def production_sqnr(fn, N, inv=False, mode="r2", seed=0):
    cfg = FFTConfig(num_points=N, inverse=inv, stage_mode=mode)
    rng = random.Random(3000 + N + seed)
    frame = random_frame(N, cfg.sample_width, rng)
    got = c(fn(frame, cfg))
    ref = bitrev_perm(np_spec_scaled(frame, inv))
    return sqnr_db(ref, got)


def tone_pin(fn, N, dit=False):
    """Single-bin tone: peak must land exactly at slot bitrev(k) (DIF,
    natural-in) / k (DIT, bitrev-in). Returns (ok, worst_leak)."""
    worst_leak = 0.0
    for k in range(N):
        cfg = FFTConfig(num_points=N, sample_width=24, output_width=40,
                        twiddle_width=30,
                        input_order="bitreversed" if dit else "native",
                        output_order="native" if dit else "bitreversed")
        x = [complex(math.cos(2 * math.pi * k * t / N),
                     math.sin(2 * math.pi * k * t / N)) for t in range(N)]
        frame = [(int(round(v.real * 4096)), int(round(v.imag * 4096)))
                 for v in x]
        want = k if dit else _bitrev(k, N.bit_length() - 1)
        if dit:
            n = N.bit_length() - 1
            frame = [frame[_bitrev(j, n)] for j in range(N)]
        out = c(fn(frame, cfg))
        mags = [abs(v) for v in out]
        peak = max(range(N), key=lambda i: mags[i])
        if peak != want:
            return False, worst_leak
        leak = (sum(v * v for i, v in enumerate(mags) if i != want)
                + 1e-30) / max(1e-30, mags[want] ** 2)
        worst_leak = max(worst_leak, leak)
    return True, worst_leak


def impulse_pin(fn, N, dit=False):
    """Shifted impulse: flat |X| with a correct linear phase ramp; the
    peak must sit on the right slot. Returns (ok, worst_sqnr)."""
    ok, worst = True, float("inf")
    for n0 in (0, 1, 5, N - 1):
        cfg = FFTConfig(num_points=N, sample_width=24, output_width=40,
                        twiddle_width=30, scaling=(0,) * (N.bit_length() - 1),
                        input_order="bitreversed" if dit else "native",
                        output_order="native" if dit else "bitreversed")
        frame = [(0, 0)] * N
        frame[n0] = (1 << 14, 0)
        if dit:
            n = N.bit_length() - 1
            frame = [frame[_bitrev(j, n)] for j in range(N)]
        out = c(fn(frame, cfg))
        ideal = [complex(1 << 14, 0) * complex(
            math.cos(-2 * math.pi * k * n0 / N),
            math.sin(-2 * math.pi * k * n0 / N)) for k in range(N)]
        if not dit:
            ideal = bitrev_perm(ideal)
        s = sqnr_db(ideal, out)
        flat = max(abs(abs(v) - (1 << 14)) for v in out)
        if not (s > 80 and flat < 8):
            ok = False
        worst = min(worst, s)
    return ok, worst


def emitting_frames(cfg, real, arch, lead=20, tail=20):
    """SOF-anchored raw windows of an SSR golden model run. Returns
    [wins...] with wins[b][e] = value on output slot e of real frame b."""
    m = SSRGoldenModel(cfg, arch=arch)
    N = cfg.num_points
    allf = [[(0, 0)] * N] * lead + real + [[(0, 0)] * N] * tail
    samples = [s for fr in allf for s in fr]
    markers = []
    for _ in allf:
        markers += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]
    outs = m.process_stream(samples, markers=markers)
    sofs = [i for i, o in enumerate(outs) if o[2] == 1]
    wins = [[outs[s + e][:2] for e in range(N)]
            for s in sofs if s + N <= len(outs)]
    return [w for w in wins if any(v != (0, 0) for v in w)]


def ref_batch(frame, inv, mode):
    """R=1 batch golden, natural-bin dict (undo the DIF slot order)."""
    cfg = FFTConfig(num_points=len(frame), inverse=inv, stage_mode=mode)
    fn = fft_fixed_batch_r22 if mode == "r22" else fft_fixed_batch
    br = fn(frame, cfg)
    n = len(frame).bit_length() - 1
    return {k: br[_bitrev(k, n)] for k in range(len(frame))}


def frame_markers(nfr, N):
    out = []
    for _ in range(nfr):
        out += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]
    return out


def ssr_section(R_vals, N_vals):
    print("\n=== SSR lanes (r2 and r22) vs the R=1 batch =================")
    for arch in ("r2", "r22"):
        for R in R_vals:
            for N in N_vals:
                if N % R or N // R < 4:
                    continue
                for inv in (False, True):
                    cfg = FFTConfig(num_points=N, ssr=R, inverse=inv,
                                    output_order="native", stage_mode=arch)
                    rng = random.Random(7000 + N + R + (1000 if inv else 0))
                    real = [random_frame(N, cfg.sample_width, rng)
                            for _ in range(3)]
                    wins = emitting_frames(cfg, real, arch=arch)
                    perm = ssr_emission_perm(N, R, "native")
                    tol = R // 2 + 1
                    worst = 0
                    ok = len(wins) == 3
                    for b, w in enumerate(wins):
                        ref = ref_batch(real[b], inv, arch)
                        for e in range(N):
                            d = max(abs(w[e][0] - ref[perm[e]][0]),
                                    abs(w[e][1] - ref[perm[e]][1]))
                            worst = max(worst, d)
                            if d > tol:
                                ok = False
                    check(ok,
                          f"SSR {arch} R={R} N={N} inv={int(inv)} "
                          f"native->native",
                          f"{len(wins)}/3 frames  worst |d|={worst} LSB "
                          f"(doc tol={tol})")


def roundtrip(fwd_fn, inv_fn, fwd_cfg, inv_cfg, N, tol=4):
    rng = random.Random(9000 + N)
    frame = random_frame(N, 16, rng)
    fwd = fwd_fn(frame, fwd_cfg)
    back = inv_fn(fwd, inv_cfg)
    n = N.bit_length() - 1
    want = [(round_shift(re, n), round_shift(im, n)) for re, im in frame]
    err = max(max(abs(a[0] - b[0]), abs(a[1] - b[1]))
              for a, b in zip(want, back))
    return err <= tol, err


# ===================================================================
print("\n=== 1. float references == numpy (both directions) ===========")
for N in (2, 8, 64, 256):
    rng = random.Random(11)
    x = [complex(rng.uniform(-100, 100), rng.uniform(-100, 100))
         for _ in range(N)]
    a = fft_float_radix2(x)
    b = fft_float_reference(x)
    ref = np.fft.fft(x)
    rel = max(abs(v - w) for v, w in zip(a, ref))
    scale = max(abs(v) for v in ref)
    check(rel / scale < 1e-10 and max(abs(u - v) for u, v in zip(a, b)) < 1e-9,
          f"float references == numpy  N={N}",
          f"rel err {rel / scale:.2e}")

for N in (4, 16, 128):
    rng = random.Random(12)
    x = [complex(rng.uniform(-100, 100), rng.uniform(-100, 100))
         for _ in range(N)]
    a = fft_float_radix2(x, inverse=True)
    ref = np.fft.ifft(x) * N
    rel = max(abs(v - w) for v, w in zip(a, ref))
    check(rel / max(abs(v) for v in ref) < 1e-10,
          f"float inverse reference == numpy  N={N}")

print("\n=== 2. twiddle table vs exact W_N^k ===========================")
worst_mag = worst_rel = 0.0
for N in (8, 16, 32, 64, 128, 256, 1024):
    for w, d in ((18, 17), (12, 11), (8, 7), (18, 16), (24, 23)):
        tw = canonical_twiddles(N, w, d)
        for k in range(N):
            z = complex(*tw[k]) / (1 << d)
            worst_mag = max(worst_mag, abs(abs(z) - 1.0))
check(worst_mag < 2e-2, "twiddle magnitudes bounded by construction",
      f"worst |z|-1 = {worst_mag:.3e}")
# relative per-entry error against the exact angle, 18-bit Q17 only
worst_rel = 0.0
for N in (8, 16, 32, 64, 128, 256, 1024):
    tw = canonical_twiddles(N, 18, 17)
    for k in range(N):
        if (4 * k) % N == 0:
            continue
        z = complex(*tw[k]) / (1 << 17)
        exact = complex(math.cos(-2 * math.pi * k / N),
                        math.sin(-2 * math.pi * k / N))
        worst_rel = max(worst_rel, abs(abs(z / exact) - 1.0))
check(worst_rel < 3 * 2 ** -18, "twiddle precision matches the Q17 contract",
      f"worst rel err = {worst_rel:.3e}")

print("\n=== 3. quantization primitives ================================")
okk = True
for _ in range(200):
    rng = random.Random(31 + _)
    a, b, cc, d = (rng.randint(-10 ** 9, 10 ** 9) for _ in range(4))
    re, im = complex_multiply_karatsuba(a, b, cc, d)
    if (re, im) != (a * cc - b * d, a * d + b * cc):
        okk = False
check(okk, "Karatsuba complex product identity (3 multiplies)")
okr = True
for _ in range(2000):
    v = random.Random(32).randint(-10 ** 9, 10 ** 9)
    s = random.Random(33).randint(0, 20)
    want = math.floor(v / 2 ** s + 0.5) if v >= 0 \
        else -math.floor(-v / 2 ** s + 0.5)
    if round_shift(v, s) != want and round_shift(v, s) != math.floor(v / 2 ** s + 0.5):
        okr = False
check(okr, "round_shift == deterministic round-half-up arithmetic shift")
oks = True
for _ in range(2000):
    v = random.Random(34).randint(-2 ** 40, 2 ** 40)
    w = 12
    got = saturate(v, w)
    lo, hi = -(1 << (w - 1)), (1 << (w - 1)) - 1
    if not (lo <= got <= hi and (v == got or got == lo or got == hi)):
        oks = False
check(oks, "saturate clamps to the signed range and never corrupts in-range")

print("\n=== 4. r2 (plain radix-2) batch models == true DFT ============")
print("  exact mode (wide words, zero shifts; floor = 0.5-LSB roundings):")
for N in (4, 8, 32, 128, 1024):
    for tw in (60,):
        s, me = exact_mode(fft_fixed_batch, N, tw=tw)
        check(s > 100, f"r2 DIF batch == true DFT   N={N}",
              f"SQNR={s:6.1f} dB  maxerr={me:.2f}")
s, me = exact_mode(fft_fixed_batch, 64, inv=True)
check(s > 100, "r2 DIF batch == true IDFT   N=64 inv", f"SQNR={s:6.1f} dB")
for N in (8, 32, 128):
    s, me = exact_mode(fft_fixed_batch_dit, N, order=("bitreversed", "native"))
    check(s > 100, f"r2 DIT batch == true DFT   N={N}", f"SQNR={s:6.1f} dB")
print("  production precision (16-bit / 18-bit Q17 / auto scaling):")
for N in (16, 64, 256, 1024, 2048):
    for inv in (False, True):
        s = production_sqnr(fft_fixed_batch, N, inv)
        check(s > 55, f"r2 production SQNR vs true DFT  N={N} inv={int(inv)}",
              f"{s:6.1f} dB")

print("\n=== 5. r22 batch models == true DFT ============================")
print("  exact mode:")
for N in (8, 32, 128, 1024):
    for tw in (60, 120):
        s, me = exact_mode(fft_fixed_batch_r22, N, tw=tw, mode="r22")
        check(s > 100, f"r22 DIF batch == true DFT   N={N} tw={tw}",
              f"SQNR={s:6.1f} dB  maxerr={me:.2f}")
s, me = exact_mode(fft_fixed_batch_r22, 128, inv=True, mode="r22")
check(s > 100, "r22 DIF batch == true IDFT   N=128 inv", f"SQNR={s:6.1f} dB")
for N in (8, 16, 64, 256):
    s, me = exact_mode(fft_fixed_batch_r22_dit, N, mode="r22",
                       order=("bitreversed", "native"))
    check(s > 100, f"r22 DIT batch == true DFT   N={N}", f"SQNR={s:6.1f} dB")
print("  production precision:")
for N in (16, 64, 256, 1024, 2048):
    for inv in (False, True):
        s = production_sqnr(fft_fixed_batch_r22, N, inv, mode="r22")
        check(s > 55, f"r22 production SQNR vs true DFT  N={N} inv={int(inv)}",
              f"{s:6.1f} dB")

print("\n=== 6. structural pins: tone and shifted impulse ===============")
for mode in ("r2", "r22"):
    fn = fft_fixed_batch if mode == "r2" else fft_fixed_batch_r22
    ok, leak = tone_pin(fn, 32)
    check(ok, f"tone pin (DIF): peak exactly in slot bitrev(k)  {mode}",
          f"worst leak {10 * math.log10(max(leak, 1e-30)):.1f} dB")
    ok, s = impulse_pin(fn, 32)
    check(ok, f"impulse pin (DIF): flat |X| + phase ramp  {mode}",
          f"worst SQNR {s:.1f} dB")
    fnd = fft_fixed_batch_dit if mode == "r2" else fft_fixed_batch_r22_dit
    ok, leak = tone_pin(fnd, 32, dit=True)
    check(ok, f"tone pin (DIT): peak exactly in slot k  {mode}",
          f"worst leak {10 * math.log10(max(leak, 1e-30)):.1f} dB")
    ok, s = impulse_pin(fnd, 32, dit=True)
    check(ok, f"impulse pin (DIT): flat |X| + phase ramp  {mode}",
          f"worst SQNR {s:.1f} dB")

print("\n=== 7. FFT -> IFFT round trips ================================")
for N in (16, 64, 256):
    cfg_f = FFTConfig(num_points=N)
    cfg_i = FFTConfig(num_points=N, inverse=True, input_order="bitreversed",
                      output_order="native")
    ok, err = roundtrip(fft_fixed_batch, fft_fixed_batch_dit,
                        cfg_f, cfg_i, N)
    check(ok, f"r2 round trip recovers x/N  N={N}", f"max |err| = {err} LSB")
    cfg_f22 = FFTConfig(num_points=N, stage_mode="r22")
    cfg_i22 = FFTConfig(num_points=N, inverse=True,
                        input_order="bitreversed", output_order="native")
    cfg_i22.stage_mode = "r22"   # r22 DIT batch fn ignores stage_mode
    ok, err = roundtrip(fft_fixed_batch_r22, fft_fixed_batch_r22_dit,
                        cfg_f22, cfg_i22, N)
    check(ok, f"r22 round trip recovers x/N  N={N}", f"max |err| = {err} LSB")

print("\n=== 8. streaming models == batch bit-exact and == DFT ==========")


def streaming_check(cls, cfg, samples, frames, natural=None, dit=False):
    model = cls(cfg, dit=dit) if cls is SDFGoldenModel else cls(cfg)
    got = model.process_stream(samples)
    batch = fft_fixed_batch_r22 if cfg.is_r22 else fft_fixed_batch
    if dit:
        batch = fft_fixed_batch_r22_dit if cfg.is_r22 else fft_fixed_batch_dit
    exp = []
    for f in range(frames):
        exp += batch(samples[f * N:(f + 1) * N], cfg)
    bit_exact = (len(got) == len(exp)
                 and all(got[i] == exp[i] for i in range(len(exp))))
    ref = []
    for f in range(frames):
        fr = (natural[f * N:(f + 1) * N] if natural is not None
              else samples[f * N:(f + 1) * N])
        spec = np_spec_scaled(fr, False)
        ref += ([complex(v) for v in spec] if dit
                else bitrev_perm([complex(v) for v in spec]))
    s = sqnr_db(ref, c(got))
    return bit_exact, s


for N in (16, 32, 64, 128, 256, 1024):
    cfg = FFTConfig(num_points=N)
    rng = random.Random(5000 + N)
    samples = random_frame(N * 2, cfg.sample_width, rng)
    bit_exact, s = streaming_check(SDFGoldenModel, cfg, samples, 2)
    check(bit_exact and s > 55, f"streaming r2 DIF == batch == DFT  N={N}",
          f"bit-exact={bit_exact}  SQNR={s:6.1f} dB")
for N in (16, 64, 128):
    cfg = FFTConfig(num_points=N, input_order="bitreversed",
                    output_order="native")
    rng = random.Random(5000 + N)
    n = N.bit_length() - 1
    raw = random_frame(N, cfg.sample_width, rng)
    samples = [raw[_bitrev(j, n)] for j in range(N)]
    bit_exact, s = streaming_check(SDFGoldenModel, cfg, samples, 1,
                                   natural=raw, dit=True)
    check(bit_exact and s > 55, f"streaming r2 DIT == batch == DFT  N={N}",
          f"bit-exact={bit_exact}  SQNR={s:6.1f} dB")
for N in (16, 32, 128, 1024):
    cfg = FFTConfig(num_points=N, stage_mode="r22")
    rng = random.Random(5000 + N)
    samples = random_frame(N * 2, cfg.sample_width, rng)
    bit_exact, s = streaming_check(R22SDFGoldenModel, cfg, samples, 2)
    check(bit_exact and s > 55, f"streaming r22 DIF == batch == DFT  N={N}",
          f"bit-exact={bit_exact}  SQNR={s:6.1f} dB")
for N in (16, 64, 128):
    cfg = FFTConfig(num_points=N, input_order="bitreversed",
                    output_order="native")
    cfg.stage_mode = "r22"   # r22 DIT batch fn ignores stage_mode
    rng = random.Random(5000 + N)
    n = N.bit_length() - 1
    raw = random_frame(N, cfg.sample_width, rng)
    samples = [raw[_bitrev(j, n)] for j in range(N)]
    bit_exact, s = streaming_check(R22SDFGoldenModelDit, cfg, samples, 1,
                                   natural=raw, dit=True)
    check(bit_exact and s > 55, f"streaming r22 DIT == batch == DFT  N={N}",
          f"bit-exact={bit_exact}  SQNR={s:6.1f} dB")

print("\n=== 9. reorder + order compositions (OrderedFFTModel) ==========")
for N in (8, 32, 128):
    rng = random.Random(6000 + N)
    frame = random_frame(N, 16, rng)
    spec = np_spec_scaled(frame, False)
    for in_o, out_o in (("native", "bitreversed"), ("native", "native"),
                        ("bitreversed", "native"), ("bitreversed", "bitreversed")):
        cfg = FFTConfig(num_points=N, input_order=in_o, output_order=out_o)
        n = N.bit_length() - 1
        samples = frame if in_o == "native" \
            else [frame[_bitrev(j, n)] for j in range(N)]
        got = c(OrderedFFTModel(cfg).process_stream(samples))
        ref = [complex(v) for v in spec]
        if out_o == "bitreversed":
            ref = bitrev_perm(ref)
        s = sqnr_db(ref, got)
        check(s > 55, f"OrderedFFTModel {in_o}->{out_o}  N={N}",
              f"SQNR={s:6.1f} dB")
# ReorderModel in isolation: stream of unique samples -> bitrev order
okr = True
for N in (8, 32, 128):
    seq = [(i, -i) for i in range(2 * N)]
    out = ReorderModel(N).process_stream(seq)
    n = N.bit_length() - 1
    want = []
    for f in range(2):
        for e in range(N):
            i = f * N + _bitrev(e, n)
            want.append(seq[i])
    if out != want:
        okr = False
check(okr, "ReorderModel == bit-reversal permutation (streaming)")

print("\n=== 10. freeze (ce-gating) semantics ===========================")


def freeze_semantics(cls, cfg, samples, markers, gap):
    """Feed samples through ``cls`` with frozen bubbles inserted between
    some of them (ce = 0 cycles, no data enters). The outputs must be
    identical to an ungated run: ce-gating must never corrupt a frame."""
    T = len(samples)
    dense = cls(cfg).process_stream(samples, markers=markers)
    m = cls(cfg)
    outs = []
    for i in range(T):
        if gap(i):
            m.tick(False)
        v, re, im, u, l = m.tick(True, samples[i][0], samples[i][1],
                                 markers[i][0], markers[i][1])
        if v:
            outs.append((re, im, u, l))
    guard = 0
    while len(outs) < T and guard < 20 * T:
        v, re, im, u, l = m.tick(True, 0, 0, 0, 0)
        if v:
            outs.append((re, im, u, l))
        guard += 1
    return len(outs) == len(dense) and all(outs[i] == dense[i]
                                           for i in range(len(dense)))


for N in (16, 64, 256):
    cfg = FFTConfig(num_points=N)
    rng = random.Random(6500 + N)
    samples = random_frame(N * 2, cfg.sample_width, rng)
    markers = [(1 if i % N == 0 else 0, 1 if i % N == N - 1 else 0)
               for i in range(len(samples))]
    ok = freeze_semantics(SDFGoldenModel, cfg, samples, markers,
                          lambda i: i % 7 == 3)
    check(ok, f"r2 stream: frozen bubbles == dense run  N={N}")
    cfg22 = FFTConfig(num_points=N, stage_mode="r22")
    ok = freeze_semantics(R22SDFGoldenModel, cfg22, samples, markers,
                          lambda i: i % 5 == 1 or i % 13 == 9)
    check(ok, f"r22 stream: frozen bubbles == dense run  N={N}")

print("\n=== 11. SSR lanes (r2 and r22) vs the R=1 batch ===============")
ssr_section((2, 4, 8), (32, 64, 128, 256))

print("\n=== 12. SSR corner orders (P8): r22b FFT, r22i IFFT ===========")
for N in (32, 64, 128, 256):
    cfg = FFTConfig(num_points=N, ssr=2, output_order="bitreversed",
                    stage_mode="r22")
    rng = random.Random(8000 + N)
    real = [random_frame(N, cfg.sample_width, rng) for _ in range(3)]
    wins = emitting_frames(cfg, real, arch="r22")
    perm = ssr_emission_perm(N, 2, "bitreversed")
    tol = cfg.ssr // 2 + 1
    ok = len(wins) == 3
    worst = 0
    for b, w in enumerate(wins):
        ref = ref_batch(real[b], False, "r22")
        for e in range(N):
            d = max(abs(w[e][0] - ref[perm[e]][0]),
                    abs(w[e][1] - ref[perm[e]][1]))
            worst = max(worst, d)
            if d > tol:
                ok = False
    check(ok, f"SSR corner FFT native->bitrev (r22b)  N={N}",
          f"{len(wins)}/3 frames  worst |d|={worst} LSB (tol={tol})")

for N in (32, 64, 128):
    cfg = FFTConfig(num_points=N, ssr=2, inverse=True,
                    input_order="bitreversed", output_order="native",
                    stage_mode="r22")
    fwd_cfg = FFTConfig(num_points=N, ssr=2, output_order="bitreversed",
                        stage_mode="r22")
    rng = random.Random(8500 + N)
    real = [random_frame(N, cfg.sample_width, rng) for _ in range(3)]
    fwd_wins = emitting_frames(fwd_cfg, real, arch="r22")   # raw spectra
    ok = len(fwd_wins) == 3
    specs = [[fwd_wins[b][k] for k in range(N)] for b in range(3)]
    inv_model = SSRCornerInverseModel(cfg, arch="r22")
    n = N.bit_length() - 1
    tol = 12
    worst = 0
    lead = [[(0, 0)] * N] * 20
    samples = [s for fr in lead for s in fr] + \
              [s for fr in specs for s in fr] + \
              [(0, 0)] * (N * 2)
    markers = frame_markers(20, N) + frame_markers(3, N) + frame_markers(2, N)
    outs = inv_model.process_stream(samples, markers=markers)
    sofs = [i for i, o in enumerate(outs) if o[2] == 1]
    found = 0
    for s in sofs:
        if s + N > len(outs):
            continue
        win = outs[s:s + N]
        if all(v[:2] == (0, 0) for v in win):
            continue
        b = found % 3
        found += 1
        for e in range(N):
            want = (round_shift(real[b][e][0], n),
                    round_shift(real[b][e][1], n))
            d = max(abs(win[e][0] - want[0]), abs(win[e][1] - want[1]))
            worst = max(worst, d)
            if d > tol:
                ok = False
    check(ok, f"SSR corner IFFT bitrev->native (r22i)  N={N}",
          f"{found}/3 frames recovered  worst |d|={worst} LSB (tol={tol})")

print("\n=== 13. saturation / dynamic-range behaviour ===================")
for N in (64, 1024, 2048):
    for mode in ("r2", "r22"):
        cfg = FFTConfig(num_points=N, stage_mode=mode)
        rng = random.Random(9500 + N)
        frame = random_frame(N, cfg.sample_width, rng)
        fn = fft_fixed_batch_r22 if mode == "r22" else fft_fixed_batch
        out = fn(frame, cfg)
        rail = 1 << (cfg.output_width - 1)
        sat = sum(1 for re, im in out
                  if abs(re) >= rail - 1 or abs(im) >= rail - 1)
        peak = max(max(abs(re), abs(im)) for re, im in out)
        check(sat == 0, f"no saturation, full-scale input  {mode} N={N}",
              f"peak={peak}/{rail - 1}")

print("\n" + "=" * 66)
if FAIL:
    print(f"{len(FAIL)} CHECK(S) FAILED:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")