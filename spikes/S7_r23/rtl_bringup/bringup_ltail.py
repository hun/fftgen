"""Both r22 leftover pairs (chained) vs the chained patched golden."""
import os, subprocess, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R22DIFStage

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
N = 8192
tw = canonical_twiddles(N, 18, 17, False)
UP = 7170 + 898 + 114
lines = [ln.split() for ln in open(os.path.join(BUILD, "gold_t2.hex"))
         if ln.strip()]

def mkpair(D, base):
    lp = _R22DIFStage(4, N, 1, 1, 17, tw, False)
    lp.D, lp.base = D, base
    lp.ram = [(0, 0)] * (2 * D)
    lp.sram = [(0, 0)] * D
    lp.dram = [(0, 0)] * D
    lp.dline = [(0, 0)] * D
    lp.pfifo = [(0, 0)] * (2 * D)
    return lp

lp0 = mkpair(4, 512)
lp1 = mkpair(1, 2048)
with open(os.path.join(BUILD, "stim_ltail.mem"), "w") as f:
    for c in range(20000):
        v = lines[c] if c < len(lines) else ("0000", "0000")
        f.write("%08x\n" % ((int(v[1], 16) << 16) | int(v[0], 16)))
with open(os.path.join(BUILD, "gold_ltail.hex"), "w") as f:
    for c in range(20000):
        v = lines[c] if c < len(lines) else ("0000", "0000")
        cur = (int(v[0], 16), int(v[1], 16))
        cur = (cur[0] - (1 << 16) if cur[0] >= (1 << 15) else cur[0],
               cur[1] - (1 << 16) if cur[1] >= (1 << 15) else cur[1])
        cur = lp0.step(cur, c - UP)
        cur = lp1.step(cur, c - UP - 13)
        f.write("%04x %04x\n" % (cur[0] & 0xFFFF, cur[1] & 0xFFFF))

def run(KP1):
    r = subprocess.run(
        ["iverilog", "-g2012", "-o", "lt.vvp", "-Pltail.KP1=%d" % KP1,
         os.path.join(ROOT, "rtl", "fft_stage_r22.v"),
         os.path.join(HERE, "tb_ltail.v")],
        cwd=BUILD, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr); sys.exit(1)
    subprocess.run(["vvp", "lt.vvp"], cwd=BUILD, capture_output=True, text=True)
    rtl = []
    for ln in open(os.path.join(BUILD, "lt.hex")):
        ln = ln.strip()
        rtl.append(None if 'x' in ln else tuple(int(x, 16) for x in ln.split()))
    gold = [tuple(int(x, 16) for x in ln.split())
            for ln in open(os.path.join(BUILD, "gold_ltail.hex")) if ln.strip()]
    best = None
    for H in range(-40, 20):
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
    return best

for KP1 in (0, 1, 2, 3):
    H, ok, tot = run(KP1)
    print(f"KP1={KP1}: H={H} {ok}/{tot}")
