"""Scan KP_L0_TRIM for the wrapper's first r22 leftover pair, using the
l1 tap (pair-0 output) vs a golden pair-0 reference stream."""
import os, random, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R23DIFStage, _R22DIFStage

N = 8192
GS = [1024, 128, 16]
TW, TD = 18, 17
T = N * 2
tw = canonical_twiddles(N, TW, TD, False)
trip_gold = [7 * g + 2 for g in GS]
UP3 = sum(trip_gold)

sts = [_R23DIFStage(m, N, 1, 1, 1, TD, tw, False) for m in range(3)]


def mkpair(D, base):
    lp = _R22DIFStage(4, N, 1, 1, TD, tw, False)
    lp.D, lp.base = D, base
    lp.ram = [(0, 0)] * (2 * D)
    lp.sram = [(0, 0)] * D
    lp.dram = [(0, 0)] * D
    lp.dline = [(0, 0)] * D
    lp.pfifo = [(0, 0)] * (2 * D)
    return lp


pair0 = mkpair(N >> 11, 1 << 9)

rng = random.Random(424242)
hi = 1 << 15
samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
           for _ in range(T)]

TAIL = UP3 + 40
gold_l1 = []
for pos in range(T + TAIL):
    src = samples[pos] if pos < T else (0, 0)
    cur = src
    up = 0
    for st in sts:
        cur = st.step(cur, pos - up)
        up += st.latency
    gold_l1.append(pair0.step(cur, pos - UP3))

with open(os.path.join(BUILD, "gold_l1.hex"), "w") as f:
    for v in gold_l1:
        f.write("%04x %04x\n" % (v[0] & 0xFFFF, v[1] & 0xFFFF))

RTLS = [os.path.join(ROOT, "rtl", "fft_stage_r23.v"),
        os.path.join(ROOT, "rtl", "fft_stage_r22.v"),
        os.path.join(ROOT, "rtl", "fft_sdf_r23.v"),
        os.path.join(HERE, "tb_core.v")]


def run_l0t(l0t):
    r = subprocess.run(
        ["iverilog", "-g2012", "-o", "scan.vvp", "-Ptb_core.INV=0",
         "-Ptb_core.L0T=%d" % l0t, "-Ptb_core.L1T=0"] + RTLS,
        cwd=BUILD, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr)
        sys.exit(1)
    subprocess.run(["vvp", "scan.vvp"], cwd=BUILD,
                   capture_output=True, text=True)
    l1 = []
    for ln in open(os.path.join(BUILD, "taps.hex")):
        w = ln.split()
        if 'x' in ln:
            l1.append(None)
        else:
            l1.append((int(w[6], 16), int(w[7], 16)))
    best = None
    for H in range(15, 50):
        ok = tot = 0
        for c in range(len(l1)):
            p = c - H
            if p < 0 or p >= len(gold_l1) or l1[c] is None:
                continue
            g = gold_l1[p]
            if not (g[0] | g[1]):
                continue
            tot += 1
            if (g[0] & 0xFFFF) == l1[c][0] and (g[1] & 0xFFFF) == l1[c][1]:
                ok += 1
        if best is None or ok > best[1]:
            best = (H, ok, tot)
    return best


for l0t in range(int(sys.argv[1]), int(sys.argv[2])):
    H, ok, tot = run_l0t(l0t)
    tag = " <== FULL" if ok == tot else ""
    print(f"L0T={l0t}: H={H} {ok}/{tot}{tag}", flush=True)
