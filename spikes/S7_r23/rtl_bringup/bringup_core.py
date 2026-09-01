"""Full-core test: fft_sdf_r23 vs the golden, parameterized by N.

The triple/leftover split is derived exactly as in the RTL (NTRIP =
the largest t in 1..3 with 3t <= NSTAGES, (NSTAGES-3t) even, and the
smallest triple G = N >> (3t) >= 8; NPAIRL = (NSTAGES-3*NTRIP)/2).
The golden chain mirrors the RTL rounding contract: NTRIP
_R23DIFStages (sigma 1,1,1) + NPAIRL patched _R22DIFStages.

env: N (default 8192), INV, NBLK (default 2), SEED (default 424242)
"""
import os, subprocess, sys, random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R23DIFStage, _R22DIFStage

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
N = int(os.environ.get("N", "8192"))
TW, TD = 18, 17
INVERSE = bool(int(os.environ.get("INV", "0")))
NBLK = int(os.environ.get("NBLK", "2"))
T = N * NBLK
NSTAGES = N.bit_length() - 1

# the NTRIP/NPAIRL derivation (mirrors rtl/fft_sdf_r23.v)
def derive():
    for t in (3, 2, 1):
        if NSTAGES >= 3 * t and (NSTAGES - 3 * t) % 2 == 0 and (N >> (3 * t)) >= 8:
            return t, (NSTAGES - 3 * t) // 2
    return 0, 0

NTRIP, NPAIRL = derive()
if NTRIP == 0:
    print(f"N={N}: no valid triple count (NSTAGES={NSTAGES}) -- not supported")
    sys.exit(3)
GS = [N >> (3 * m + 3) for m in range(NTRIP)]
print(f"N={N} NSTAGES={NSTAGES} NTRIP={NTRIP} NPAIRL={NPAIRL} GS={GS}")

tw = canonical_twiddles(N, TW, TD, INVERSE)
trip_gold = [7 * g + 2 for g in GS]

# the golden chain: NTRIP r23 triples + NPAIRL patched r22 pairs
sts = [_R23DIFStage(m, N, 1, 1, 1, TD, tw, INVERSE) for m in range(NTRIP)]

def mkpair(D, base):
    lp = _R22DIFStage(4, N, 1, 1, TD, tw, INVERSE)
    lp.D, lp.base = D, base
    lp.ram = [(0, 0)] * (2 * D)
    lp.sram = [(0, 0)] * D
    lp.dram = [(0, 0)] * D
    lp.dline = [(0, 0)] * D
    lp.pfifo = [(0, 0)] * (2 * D)
    return lp

DS = [N >> (3 * NTRIP + 2 * jj + 2) for jj in range(NPAIRL)]
STRIDES = [1 << (3 * NTRIP + 2 * jj) for jj in range(NPAIRL)]
los = [mkpair(DS[jj], STRIDES[jj]) for jj in range(NPAIRL)]
LO_OFFS = []
acc = sum(trip_gold)
for jj in range(NPAIRL):
    LO_OFFS.append(acc)
    acc += 3 * DS[jj] + 1
UP3 = sum(trip_gold)
GOLD_LAT = acc
TAIL = GOLD_LAT + 8

rng = random.Random(int(os.environ.get("SEED", "424242")))
hi = 1 << 15
samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
           for _ in range(T)]

# ---------------- stimulus + ROMs ----------------
with open(os.path.join(BUILD, "stim_core.mem"), "w") as f:
    for pos in range(T + GOLD_LAT + 80):
        re, im = samples[pos] if pos < T else (0, 0)
        f.write("%08x\n" % (((im & 0xFFFF) << 16) | (re & 0xFFFF)))

def rom23(g, base):
    layout = [(0, 2), (g, 6), (3 * g, 1), (4 * g, 5),
              (5 * g, 3), (6 * g, 7), (7 * g, 4)]
    mask = (1 << TW) - 1
    words = [0] * (8 * g)
    for b, k in layout:
        for gg in range(g):
            re, im = tw[(k * gg * base) % N]
            words[b + gg] = ((re & mask) << TW) | (im & mask)
    return words

for m in range(3):
    g = N >> (3 * m + 3)
    with open(os.path.join(BUILD, f"fft_tw_r23_t{m}.mem"), "w") as f:
        for w in rom23(g, 1 << (3 * m)):
            f.write("%05x\n" % (w & ((1 << (2 * TW)) - 1)))

# one concatenated leftover ROM: pair jj's 3*D slice at the cumulative base
lwords = [0] * sum(3 * d for d in DS)
for jj in range(NPAIRL):
    base = sum(3 * DS[i] for i in range(jj))
    mask = (1 << TW) - 1
    i = base
    for which in (1, 2, 3):
        for g in range(DS[jj]):
            re, im = tw[(which * g * STRIDES[jj]) % N]
            lwords[i] = ((re & mask) << TW) | (im & mask)
            i += 1
with open(os.path.join(BUILD, "fft_tw_r22_l.mem"), "w") as f:
    for w in lwords:
        f.write("%05x\n" % (w & ((1 << (2 * TW)) - 1)))

# ---------------- golden outputs ----------------
outs = []
for pos in range(T + TAIL):
    src = samples[pos] if pos < T else (0, 0)
    cur = src
    up = 0
    for st in sts:
        cur = st.step(cur, pos - up)
        up += st.latency
    for jj, st in enumerate(los):
        cur = st.step(cur, pos - LO_OFFS[jj])
    outs.append(cur)

# ---------------- RTL ----------------
def run():
    pack = sum(1 << (2 * s) for s in range(NSTAGES))
    r = subprocess.run(
        ["iverilog", "-g2012", "-o", "core.vvp",
         "-Ptb_core.INV=%d" % int(INVERSE),
         "-Ptb_core.NUM_POINTS=%d" % N,
         "-Ptb_core.NBLK=%d" % NBLK,
         "-Ptb_core.PACK=%d" % pack,
         os.path.join(ROOT, "rtl", "fft_stage_r23.v"),
         os.path.join(ROOT, "rtl", "fft_stage_r22.v"),
         os.path.join(ROOT, "rtl", "fft_sdf_r23.v"),
         os.path.join(HERE, "tb_core.v")],
        cwd=BUILD, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr); sys.exit(1)
    subprocess.run(["vvp", "core.vvp"], cwd=BUILD,
                   capture_output=True, text=True)
    with open(os.path.join(BUILD, "out.hex")) as f:
        rtl = []
        for ln in f:
            ln = ln.strip()
            rtl.append(None if 'x' in ln else
                       tuple(int(x, 16) for x in ln.split()))
    trip_rtl = [7 * g + 12 for g in GS]
    pair_rtl = [3 * d + 9 for d in DS]
    rtl_lat = sum(trip_rtl) + sum(pair_rtl) + 1
    h_est = rtl_lat - GOLD_LAT + 1
    best = None
    for H in range(max(0, h_est - 4), h_est + 8):
        ok = tot = 0
        for c in range(len(rtl)):
            p = c - H
            if p < 0 or p >= len(outs) or rtl[c] is None:
                continue
            g = outs[p]
            if not (g[0] | g[1]):
                continue
            tot += 1
            a = rtl[c]
            if (g[0] & 0xFFFF) == a[0] and (g[1] & 0xFFFF) == a[1]:
                ok += 1
        if best is None or ok > best[1]:
            best = (H, ok, tot)
    return best, rtl

if __name__ == "__main__":
    (H, ok, tot), rtl = run()
    print(f"core (N={N}, INV={int(INVERSE)}, NBLK={NBLK}): "
          f"H={H} {ok}/{tot} = {100.0*ok/tot:.2f}%")
    if ok == tot:
        print("FULL CORE MATCHES GOLDEN bit-exactly")
    else:
        from collections import Counter
        cen = Counter()
        shown = 0
        for c in range(len(rtl)):
            p = c - H
            if p < 0 or p >= len(outs) or rtl[c] is None:
                continue
            g = outs[p]
            if not (g[0] | g[1]):
                continue
            bad = (g[0] & 0xFFFF) != rtl[c][0] or (g[1] & 0xFFFF) != rtl[c][1]
            cen[(p // N, "ok" if bad == 0 else "BAD")] += 1
            if bad and shown < 6:
                print(f"  c={c} pos={p} k={p%N}: rtl={rtl[c]} gold={g}")
                shown += 1
        print("  census:", dict(sorted(cen.items())))
