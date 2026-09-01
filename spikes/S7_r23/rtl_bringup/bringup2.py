"""Two-stage r23 chain vs the golden (_R23DIFStage m=0 -> m=1)."""
import os, subprocess, sys, random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R23DIFStage

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
N = 1024
G1, G2 = 128, 16
TW, TD = 18, 17
INVERSE = bool(int(os.environ.get("INV", "0")))
NBLK = int(os.environ.get("NBLK", "4"))
T = N * NBLK
LAT1 = 7 * G1 + 2
TAIL = LAT1 + LAT1 + 32
# patch the TB's clock count
import re
_tb = os.path.join(HERE, "tb_chain.v")
_tsrc = open(_tb).read()
_tsrc = re.sub(r"localparam integer TCLOCKS = \d+;",
               "localparam integer TCLOCKS = %d;" % (T + TAIL), _tsrc)
open(_tb, "w").write(_tsrc)

tw = canonical_twiddles(N, TW, TD, INVERSE)
st1 = _R23DIFStage(0, N, 1, 1, 1, TD, tw, INVERSE)
st2 = _R23DIFStage(1, N, 1, 1, 1, TD, tw, INVERSE)
rng = random.Random(77003)
hi = 1 << 15
samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
           for _ in range(T)]

with open(os.path.join(BUILD, "stim.mem"), "w") as f:
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

for name, g, b in (("fft_tw_r23_g128.mem", G1, 1),
                   ("fft_tw_r23_g16.mem", G2, 8)):
    with open(os.path.join(BUILD, name), "w") as f:
        for w in rom(g, b):
            f.write("%05x\n" % (w & ((1 << (2 * TW)) - 1)))

outs = []
for pos in range(T + TAIL):
    src = samples[pos] if pos < T else (0, 0)
    cur = st1.step(src, pos)
    cur = st2.step(cur, pos - LAT1)
    outs.append(cur)

def run_kp(KP):
    r = subprocess.run(
        ["iverilog", "-g2012", "-o", "chain.vvp",
         "-Ptb_chain.INV=%d" % int(INVERSE),
         "-Ptb_chain.KP2=%d" % KP,
         os.path.join(ROOT, "rtl", "fft_stage_r23.v"),
         os.path.join(HERE, "tb_chain.v")],
        cwd=BUILD, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr); sys.exit(1)
    subprocess.run(["vvp", "chain.vvp"], cwd=BUILD,
                   capture_output=True, text=True)
    with open(os.path.join(BUILD, "out.hex")) as f:
        rtl = []
        for ln in f:
            ln = ln.strip()
            rtl.append(None if 'x' in ln else
                       tuple(int(x, 16) for x in ln.split()))
    best = None
    for H in range(0, 48):
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
    return best

KP0 = (-LAT1) % (8 * G2)
best = None
for KPtry in range(108, 124):
    H, ok, tot = run_kp(KPtry)
    print(f"  KP={KPtry}: H={H} {ok}/{tot}")
    if best is None or ok > best[2]:
        best = (KPtry, H, ok, tot)
KP, H, ok, tot = best
# re-run the best KP and dump mismatches if any
r = subprocess.run(
    ["iverilog", "-g2012", "-o", "chain.vvp",
     "-Ptb_chain.INV=%d" % int(INVERSE), "-Ptb_chain.KP2=%d" % KP,
     os.path.join(ROOT, "rtl", "fft_stage_r23.v"),
     os.path.join(HERE, "tb_chain.v")],
    cwd=BUILD, capture_output=True, text=True)
subprocess.run(["vvp", "chain.vvp"], cwd=BUILD, capture_output=True, text=True)
with open(os.path.join(BUILD, "out.hex")) as f:
    rtl = []
    for ln in f:
        ln = ln.strip()
        rtl.append(None if 'x' in ln else
                   tuple(int(x, 16) for x in ln.split()))
if ok != tot:
    shown = 0
    for c in range(len(rtl)):
        p = c - H
        if p < 0 or p >= len(outs) or rtl[c] is None:
            continue
        g = outs[p]
        a = rtl[c]
        if (g[0] & 0xFFFF) != a[0] or (g[1] & 0xFFFF) != a[1]:
            print(f"  c={c} pos={p}: rtl={a} gold={g}")
            shown += 1
            if shown >= 8:
                break
print(f"2-stage chain (INV={int(INVERSE)}, NBLK={NBLK}, KP={KP}): "
      f"H={H} {ok}/{tot} = {100.0*ok/tot:.2f}%")
if ok == tot:
    print("CHAIN MATCHES GOLDEN bit-exactly")
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
            if shown >= 8:
                break
