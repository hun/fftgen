"""S7 bring-up: one fft_stage_r23 vs golden._R23DIFStage, per-cycle diff.

Runs the golden stage over the same stimulus, dumps both streams, and
scans the latency offset H (the RTL may add a uniform operand-phase
shift). Reports the first mismatches with model-phase/class decode.
"""
import os, subprocess, sys, random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R23DIFStage, _q8

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
G = int(os.environ.get("DEPTH", "128"))
W, TW, TD = 16, 18, 17
N = 8 * G
NBLK = int(os.environ.get("NBLK", "4"))
T = N * NBLK
LAT = 7 * G + 2
INVERSE = bool(int(os.environ.get("INV", "0")))

def main():
    os.makedirs(BUILD, exist_ok=True)
    tw = canonical_twiddles(N, TW, TD, INVERSE)
    st = _R23DIFStage(0, N, 1, 1, 1, TD, tw, INVERSE)
    rng = random.Random(int(os.environ.get('SEED', '20260214')))
    hi = 1 << (W - 1)
    samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
               for _ in range(T)]
    MODE = os.environ.get("STIM", "random")
    if MODE == "impulse":
        samples = [(0, 0)] * T
        samples[0] = (1, 0)

    # golden stream: outs[pos] = the value RETURNED at step pos
    outs = []
    for pos in range(T + LAT + 4):
        src = samples[pos] if pos < T else (0, 0)
        outs.append(st.step(src, pos))

    # stimulus file: {im, re} words, zeros past T
    with open(os.path.join(BUILD, "stim.mem"), "w") as f:
        for pos in range(T + LAT + 4):
            re, im = samples[pos] if pos < T else (0, 0)
            f.write("%08x\n" % (((im & 0xFFFF) << 16) | (re & 0xFFFF)))

    # twiddle ROM (the interleaved class-base layout)
    layout = [(0, 2), (G, 6), (3 * G, 1), (4 * G, 5),
              (5 * G, 3), (6 * G, 7), (7 * G, 4)]
    mask = (1 << TW) - 1
    words = [0] * (8 * G)
    for base, k in layout:
        for g in range(G):
            re, im = tw[(k * g) % N]
            words[base + g] = ((re & mask) << TW) | (im & mask)
    with open(os.path.join(BUILD, "fft_twiddles_r23.mem"), "w") as f:
        for w_ in words:
            f.write("%05x\n" % (w_ & ((1 << (2 * TW)) - 1)))

    # run the TB
    r = subprocess.run(
        ["iverilog", "-g2012", "-o", "tb.vvp",
         "-Ptb_stage.INV=%d" % int(INVERSE),
         "-Ptb_stage.DEPTH=%d" % G,
         os.path.join(ROOT, "rtl", "fft_stage_r23.v"),
         os.path.join(HERE, "tb_stage.v")],
        cwd=BUILD, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr); sys.exit(1)
    r = subprocess.run(["vvp", "tb.vvp"], cwd=BUILD,
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr); sys.exit(1)

    with open(os.path.join(BUILD, "out.hex")) as f:
        rtl = []
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            if 'x' in ln or 'X' in ln:
                rtl.append(None)
            else:
                rtl.append(tuple(int(x, 16) for x in ln.split()))
    print("rtl clocks:", len(rtl),
          " X-clocks:", sum(1 for v in rtl if v is None))

    # first-nonzero alignment (both streams are 0 during pipeline fill)
    def firstnz(seq):
        for i, v in enumerate(seq):
            if v is not None and (v[0] & 0xFFFF) | (v[1] & 0xFFFF):
                return i
        return -1
    fn_rtl = firstnz(rtl)
    fn_gold = next(p for p in range(len(outs))
                   if outs[p][0] | outs[p][1])
    print(f"first nonzero: rtl c={fn_rtl}, golden pos={fn_gold} "
          f"(=> H~{fn_rtl - fn_gold})")

    # offset scan anchored on the first-nonzero alignment, scored ONLY
    # on nonzero golden positions (zero-fill alignment is meaningless)
    best = None
    for H in range(max(0, fn_rtl - fn_gold - 3), fn_rtl - fn_gold + 4):
        ok = tot = 0
        for c in range(len(rtl)):
            p = c - H
            if p < 0 or p >= len(outs) or rtl[c] is None:
                continue
            gr, gi = outs[p]
            if not (gr | gi):
                continue
            tot += 1
            ar, ai = rtl[c]
            if (gr & 0xFFFF) == ar and (gi & 0xFFFF) == ai:
                ok += 1
        if best is None or ok > best[1]:
            best = (H, ok, tot)
    H, ok, tot = best
    print(f"best offset H={H}: {ok}/{tot} positions match "
          f"({100.0*ok/tot:.2f}%)")
    if ok == tot:
        print("STAGE MATCHES GOLDEN bit-exactly")
        return
    # per-(block, member) mismatch census
    from collections import Counter
    bad = Counter(); good = Counter()
    for c in range(len(rtl)):
        p = c - H
        if p < 0 or p >= len(outs) or rtl[c] is None:
            continue
        gr, gi = outs[p]
        ar, ai = rtl[c]
        blk = p // (8 * G)
        m = (((p - 1) % (8 * G)) // G + 1) % 8
        key = (blk, m)
        (bad if (gr & 0xFFFF) != ar or (gi & 0xFFFF) != ai
         else good)[key] += 1
    print("per-(block,member) ok/bad:")
    for key in sorted(set(bad) | set(good)):
        print(f"  blk {key[0]} member {key[1]}: {good[key]} ok, "
              f"{bad[key]} bad")
    # first mismatches with phase decode
    shown = 0
    lo = int(os.environ.get("POS_LO", 0))
    hi = int(os.environ.get("POS_HI", 1 << 60))
    for c in range(len(rtl)):
        p = c - H
        if p < lo or p >= hi:
            continue
        if p < 0 or p >= len(outs) or rtl[c] is None:
            continue
        gr, gi = outs[p]
        ar, ai = rtl[c]
        if (gr & 0xFFFF) != ar or (gi & 0xFFFF) != ai:
            k = p % (8 * G)
            gpos = p % G
            cls = k // G
            print(f"  c={c} pos={p} k={k} (slot {cls}, g={gpos}): "
                  f"rtl=({ar&0xffff:04x},{ai&0xffff:04x}) "
                  f"gold=({gr&0xffff:04x},{gi&0xffff:04x}) "
                  f"raw_gold=({gr},{gi})")
            shown += 1
            if shown >= 12:
                break

# patch the TB's clock count to cover stim + the golden latency tail
TCLKES = T + LAT + 4
tb_path = os.path.join(HERE, "tb_stage.v")
tb = open(tb_path).read()
import re
tb = re.sub(r"localparam integer TCLOCKS = \d+;",
            "localparam integer TCLOCKS = %d;" % TCLKES, tb)
open(tb_path, "w").write(tb)

if __name__ == "__main__":
    main()
