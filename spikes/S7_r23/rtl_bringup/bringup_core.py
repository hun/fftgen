"""Full-core test: fft_sdf_r23 (3 triples + 2 r22 leftover pairs) vs the
golden (3 _R23DIFStage + 4 _SDFStage leftovers with parity preloads)."""
import os, subprocess, sys, random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R23DIFStage, _R22DIFStage

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
N = 8192
GS = [1024, 128, 16]          # triple Gs (m = 0,1,2)
TW, TD = 18, 17
INVERSE = bool(int(os.environ.get("INV", "0")))
NBLK = int(os.environ.get("NBLK", "2"))
T = N * NBLK

tw = canonical_twiddles(N, TW, TD, INVERSE)
trip_gold = [7 * g + 2 for g in GS]
LATS = trip_gold + [N >> (s + 1) + 10 for s in range(9, 13)]  # unused form
# golden r2 leftover: _SDFStage latency = D + NLAYERS (NLAYERS=10)
TAIL = sum(trip_gold) + 4 * 12 + 40

# ---------------- golden chain ----------------
# 3 r23 triples + the r22-pair tail (the core's rounding contract)
sts = [_R23DIFStage(m, N, 1, 1, 1, TD, tw, INVERSE) for m in range(3)]

def mkpair(D, base):
    lp = _R22DIFStage(4, N, 1, 1, TD, tw, INVERSE)
    lp.D, lp.base = D, base
    lp.ram = [(0, 0)] * (2 * D)
    lp.sram = [(0, 0)] * D
    lp.dram = [(0, 0)] * D
    lp.dline = [(0, 0)] * D
    lp.pfifo = [(0, 0)] * (2 * D)
    return lp

UP3 = sum(trip_gold)
los = [mkpair(N >> 11, 1 << 9), mkpair(N >> 13, 1 << 11)]
LO_OFFS = [UP3, UP3 + 3 * (N >> 11) + 1]

rng = random.Random(424242)
hi = 1 << 15
samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
           for _ in range(T)]

# ---------------- stimulus + ROMs ----------------
with open(os.path.join(BUILD, "stim_core.mem"), "w") as f:
    for pos in range(T + TAIL):
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

def rom22(stride, D):
    mask = (1 << TW) - 1
    words = []
    for which in (1, 2, 3):
        for g in range(D):
            re, im = tw[(which * g * stride) % N]
            words.append(((re & mask) << TW) | (im & mask))
    return words

for name, g, b in (("fft_tw_r23_t0.mem", GS[0], 1),
                   ("fft_tw_r23_t1.mem", GS[1], 8),
                   ("fft_tw_r23_t2.mem", GS[2], 64)):
    with open(os.path.join(BUILD, name), "w") as f:
        for w in rom23(g, b):
            f.write("%05x\n" % (w & ((1 << (2 * TW)) - 1)))
# the r22 ROM is NPTS-sized (N words); the pair's 3 slices sit at
# [0, 3D) with ROM_BASE=0, the rest zero-filled
for name, stride, D in (("fft_tw_r22_l0.mem", 1 << 9, 4),
                        ("fft_tw_r22_l1.mem", 1 << 11, 1)):
    words = [0] * N
    sl = rom22(stride, D)
    for i, w in enumerate(sl):
        words[i] = w
    with open(os.path.join(BUILD, name), "w") as f:
        for w in words:
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
    for j, st in enumerate(los):
        cur = st.step(cur, pos - LO_OFFS[j])
    outs.append(cur)

# ---------------- RTL ----------------
def run(L0T, L1T):
    r = subprocess.run(
        ["iverilog", "-g2012", "-o", "core.vvp",
         "-Ptb_core.INV=%d" % int(INVERSE),
         "-Ptb_core.L0T=%d" % L0T, "-Ptb_core.L1T=%d" % L1T,
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
    best = None
    for H in range(20, 70):
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
    L0T = int(os.environ.get("L0T", "0"))
    L1T = int(os.environ.get("L1T", "0"))
    (H, ok, tot), rtl = run(L0T, L1T)
    print(f"core (INV={int(INVERSE)}, NBLK={NBLK}, L0T={L0T}, L1T={L1T}): "
          f"H={H} {ok}/{tot} = {100.0*ok/tot:.2f}%")
    if ok == tot:
        print("FULL CORE MATCHES GOLDEN bit-exactly")
    else:
        shown = 0
        for c in range(len(rtl)):
            p = c - H
            if p < 0 or p >= len(outs) or rtl[c] is None:
                continue
            g = outs[p]
            if not (g[0] | g[1]):
                continue
            a = rtl[c]
            if (g[0] & 0xFFFF) != a[0] or (g[1] & 0xFFFF) != a[1]:
                print(f"  c={c} pos={p}: rtl={a} gold={g}")
                shown += 1
                if shown >= 6:
                    break
