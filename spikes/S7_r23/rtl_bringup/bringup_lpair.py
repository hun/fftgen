"""Isolated r22-leftover-pair test vs the golden leftover _SDFStages."""
import os, subprocess, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _SDFStage

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
N = 8192
TW = 18
TW_FILES = sys.argv[1] if len(sys.argv) > 1 else "l0"   # l0 (D=4, str 2^9)
CFG = {"l0": dict(D=4, stride=1 << 9, s=(9, 10), tail=0),
       "l1": dict(D=1, stride=1 << 11, s=(11, 12), tail=0)}
cfg = CFG[TW_FILES]

lines = [ln.split() for ln in open(os.path.join(BUILD, "gold_t2.hex"))
         if ln.strip()]
with open(os.path.join(BUILD, "stim_lpair.mem"), "w") as f:
    for c in range(20000):
        q = c - cfg["tail"]
        v = lines[q] if 0 <= q < len(lines) else ("0000", "0000")
        f.write("%08x\n" % ((int(v[1], 16) << 16) | int(v[0], 16)))

mask = (1 << TW) - 1
tw = canonical_twiddles(N, TW, 17, False)
words = [0] * N
i = 0
for which in (1, 2, 3):
    for g in range(cfg["D"]):
        re, im = tw[(which * g * cfg["stride"]) % N]
        words[i] = ((re & mask) << TW) | (im & mask)
        i += 1
with open(os.path.join(BUILD, "fft_tw_r22_lx.mem"), "w") as f:
    for w in words:
        f.write("%05x\n" % (w & ((1 << (2 * TW)) - 1)))

# golden: the two leftover r2 stages, preloaded, fed the same stream
lat = 7170 + 898 + 114
los = []
for s in cfg["s"]:
    D = N >> (s + 1)
    tw_slice = [tw[(j << s) % N] for j in range(D)]
    st = _SDFStage(s, N, 1, 17, tw_slice, dit=False)
    for _ in range((-lat) % (2 * D)):
        st.step(0, 0)
    los.append(st)
    lat += D + _SDFStage.NLAYERS
with open(os.path.join(BUILD, "gold_lp.hex"), "w") as f:
    for c in range(20000):
        q = c - cfg["tail"]
        v = lines[q] if 0 <= q < len(lines) else ("0000", "0000")
        cur = (int(v[0], 16), int(v[1], 16))
        cur = (cur[0] - (1 << 16) if cur[0] >= (1 << 15) else cur[0],
               cur[1] - (1 << 16) if cur[1] >= (1 << 15) else cur[1])
        for st in los:
            cur = st.step(cur[0], cur[1])
        f.write("%04x %04x\n" % (cur[0] & 0xFFFF, cur[1] & 0xFFFF))

def run(KP):
    r = subprocess.run(
        ["iverilog", "-g2012", "-o", "lp.vvp",
         "-Plpair.KP=%d" % KP, "-Plpair.D=%d" % cfg["D"],
         "-Plpair.STR=%d" % cfg["stride"],
         os.path.join(ROOT, "rtl", "fft_stage_r22.v"),
         os.path.join(HERE, "tb_lpair.v")],
        cwd=BUILD, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr); sys.exit(1)
    subprocess.run(["vvp", "lp.vvp"], cwd=BUILD, capture_output=True, text=True)
    rtl = []
    for ln in open(os.path.join(BUILD, "lp.hex")):
        ln = ln.strip()
        rtl.append(None if 'x' in ln else
                   tuple(int(x, 16) for x in ln.split()))
    gold = [tuple(int(x, 16) for x in ln.split())
            for ln in open(os.path.join(BUILD, "gold_lp.hex")) if ln.strip()]
    best = None
    for H in range(-60, 20):
        ok = tot = 0
        for c in range(len(rtl)):
            p = c - H
            if p < 0 or p >= len(gold) or rtl[c] is None:
                continue
            g = gold[p]
            if not (g[0] | g[1]):
                continue
            tot += 1
            if g == rtl[c]:
                ok += 1
        if best is None or ok > best[1]:
            best = (H, ok, tot)
    return best, rtl, gold

(H, ok, tot), rtl, gold = run(10)
print(f"KP=10: H={H} {ok}/{tot}")
from collections import Counter
cnt = Counter()
first = {}
for c in range(len(rtl)):
    p = c - H
    if p < 0 or p >= len(gold) or rtl[c] is None:
        continue
    g = gold[p]
    if not (g[0] | g[1]):
        continue
    bad = (g != rtl[c])
    cnt[bad] += 1
    if bad and (p % 16) not in first:
        first[p % 16] = (c, p, rtl[c], g)
print("ok/bad:", dict(cnt))
for ph, v in sorted(first.items()):
    print(f"  phase {ph}: c={v[0]} rtl={v[2]} gold={v[3]}")
