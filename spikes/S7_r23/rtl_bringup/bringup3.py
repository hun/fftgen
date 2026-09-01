"""Three-stage r23 chain (N=8192 triple set: G=1024,128,16) vs the golden."""
import os, subprocess, sys, random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R23DIFStage

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
N = 8192
GS = [1024, 128, 16]
TW, TD = 18, 17
INVERSE = bool(int(os.environ.get("INV", "0")))
NBLK = int(os.environ.get("NBLK", "2"))
T = N * NBLK
LATS = [7 * g + 2 for g in GS]
H = 8
TAIL = sum(LATS) + 3 * H + 16

tw = canonical_twiddles(N, TW, TD, INVERSE)
sts = [_R23DIFStage(m, N, 1, 1, 1, TD, tw, INVERSE) for m in range(3)]
rng = random.Random(918273)
hi = 1 << 15
samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
           for _ in range(T)]

with open(os.path.join(BUILD, "stim3.mem"), "w") as f:
    for pos in range(T + TAIL):
        re, im = samples[pos] if pos < T else (0, 0)
        f.write("%08x\n" % (((im & 0xFFFF) << 16) | (re & 0xFFFF)))

def rom(g, base):
    layout = [(0, 2), (g, 6), (3 * g, 1), (4 * g, 5),
              (5 * g, 3), (6 * g, 7), (7 * g, 4)]
    mask = (1 << TW) - 1
    words = [0] * (8 * g)
    for b, k in layout:
        for gg in range(g):
            re, im = tw[(k * gg * base) % N]
            words[b + gg] = ((re & mask) << TW) | (im & mask)
    return words

for i, g in enumerate(GS):
    with open(os.path.join(BUILD, "fft_tw_r23_g%d%s.mem" %
                           (g, "b" if i else "")), "w") as f:
        for w in rom(g, 8 ** i):
            f.write("%05x\n" % (w & ((1 << (2 * TW)) - 1)))

outs = []
for pos in range(T + TAIL):
    src = samples[pos] if pos < T else (0, 0)
    cur = src
    up = 0
    for st in sts:
        cur = st.step(cur, pos - up)
        up += st.latency
    outs.append(cur)

# K_PRELOAD by the validated rule: -(sum upstream lat + j*H + j) mod 8G
KPs = []
acc = 0
for j, g in enumerate(GS[:-1]):
    acc += LATS[j] + H + 3
    KPs.append((-acc) % (8 * GS[j + 1]))
print("KPs:", KPs, " (total H = %d)" % (len(GS) * H + len(GS) - 1))

r = subprocess.run(
    ["iverilog", "-g2012", "-o", "chain3.vvp",
     "-Ptb_chain3.INV=%d" % int(INVERSE),
     "-Ptb_chain3.KP2=%d" % KPs[0], "-Ptb_chain3.KP3=%d" % KPs[1],
     os.path.join(ROOT, "rtl", "fft_stage_r23.v"),
     os.path.join(HERE, "tb_chain3.v")],
    cwd=BUILD, capture_output=True, text=True)
if r.returncode:
    print(r.stdout + r.stderr); sys.exit(1)
subprocess.run(["vvp", "chain3.vvp"], cwd=BUILD, capture_output=True, text=True)

with open(os.path.join(BUILD, "out.hex")) as f:
    rtl = []
    for ln in f:
        ln = ln.strip()
        rtl.append(None if 'x' in ln else tuple(int(x, 16) for x in ln.split()))

best = None
for Hh in range(0, 48):
    ok = tot = 0
    for c in range(len(rtl)):
        p = c - Hh
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
        best = (Hh, ok, tot)
Hh, ok, tot = best
print(f"3-stage chain (INV={int(INVERSE)}, NBLK={NBLK}): "
      f"H={Hh} {ok}/{tot} = {100.0*ok/tot:.2f}%")
if ok == tot:
    print("3-STAGE CHAIN MATCHES GOLDEN bit-exactly")
else:
    shown = 0
    for c in range(len(rtl)):
        p = c - Hh
        if p < 0 or p >= len(outs) or rtl[c] is None:
            continue
        g = outs[p]
        a = rtl[c]
        if (g[0] & 0xFFFF) != a[0] or (g[1] & 0xFFFF) != a[1]:
            print(f"  c={c} pos={p}: rtl={a} gold={g}")
            shown += 1
            if shown >= 8:
                break
