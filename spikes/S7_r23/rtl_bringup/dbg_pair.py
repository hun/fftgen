"""Pair-0 core-tap debug: replay the RTL pair input through the golden
_R22DIFStage and diff per-step internals (dram/dline reads, output).

The RTL pair's k counter = (KPRE + c) mod 4D; the golden step index
s = c - OFFS with OFFS ~ (upstream data delay + LO_OFFS). Scan OFFS on
the OFFS = 13 + 256k grid (phase identity) +/- neighborhood.

env: N (2048), INV (0)
reads build/taps.hex (fields 10,11 = pair-0 in; 2,3 = pair-0 out)
      build/pairdbg.txt (PAIR lines from tb_core.v)
"""
import os, re, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R22DIFStage

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
N = int(os.environ.get("N", "2048"))
INV = int(os.environ.get("INV", "0"))
TW, TD = 18, 17
NSTAGES = N.bit_length() - 1

def derive():
    for t in (3, 2, 1):
        if NSTAGES >= 3 * t and (NSTAGES - 3 * t) % 2 == 0 and (N >> (3 * t)) >= 8:
            return t, (NSTAGES - 3 * t) // 2
    return 0, 0

NTRIP, NPAIRL = derive()
DS = [N >> (3 * NTRIP + 2 * jj + 2) for jj in range(NPAIRL)]
STRIDES = [1 << (3 * NTRIP + 2 * jj) for jj in range(NPAIRL)]
tw = canonical_twiddles(N, TW, TD, bool(INV))

def mkpair(D, base):
    lp = _R22DIFStage(4, N, 1, 1, TD, tw, bool(INV))
    lp.D, lp.base = D, base
    lp.ram = [(0, 0)] * (2 * D)
    lp.sram = [(0, 0)] * D
    lp.dram = [(0, 0)] * D
    lp.dline = [(0, 0)] * D
    lp.pfifo = [(0, 0)] * (2 * D)
    return lp

def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v

def fld(p, i):
    return None if 'x' in p[i] else s16(int(p[i], 16))

tin, tout = [], []
for ln in open(os.path.join(BUILD, "taps.hex")):
    p = ln.split()
    tin.append((fld(p, 10), fld(p, 11)))
    tout.append((fld(p, 2), fld(p, 3)))

pat = re.compile(r"PAIR c=(\d+) k1=(-?\d+) dr=(-?\w+),(-?\w+) dl=(-?\w+),(-?\w+) "
                 r"sr=(-?\w+),(-?\w+) mr=(-?\w+),(-?\w+) tr=(-?\w+),(-?\w+) "
                 r"gr=(\d+) pfw=(\d+),(\d) gsd=(\d) gdl=(\d) out=(-?\w+),(-?\w+)")
rtl = []
for ln in open(os.path.join(BUILD, "pairdbg.txt")):
    m = pat.match(ln)
    if m:
        g = m.groups()
        rtl.append(dict(c=int(g[0]), k1=int(g[1]),
                        dr=(g[2], g[3]), dl=(g[4], g[5]),
                        mr=(g[8], g[9]), tr=(g[10], g[11]), gr=int(g[12]),
                        pwp=int(g[13]), gwp=int(g[14]),
                        out=(g[16], g[17])))
CEND = len(rtl)
def rnum(v):
    return None if 'x' in v else int(v)

D = DS[0]
KPRE = rtl[0]['k1'] % (4 * D)

def run_gold(OFFS):
    gp = mkpair(D, STRIDES[0])
    gold = []
    for s in range(CEND - OFFS):
        c = s + OFFS
        x = tin[c]
        x = (0, 0) if x[0] is None else x
        k = s % (4 * D)
        rec = dict(k=k, c=c,
                   d0=gp.dram[gp.sp] if k < 2 * D else None,
                   d1=gp.dline[gp.sp] if k < 2 * D else None)
        rec['ret'] = gp.step(x, s)
        gold.append(rec)
    return gold

# scan OFFS near the phase-identity grid
best = None
base_offs = (-KPRE) % (4 * D)
for blk in range(8, 20):
    OFFS = base_offs + blk * (4 * D)
    gold = run_gold(OFFS)
    ok = tot = 0
    for s, rec in enumerate(gold):
        c = rec['c']
        r = tout[c]
        if r[0] is None or not (rec['ret'][0] or rec['ret'][1]):
            continue
        tot += 1
        if r == rec['ret']:
            ok += 1
    if best is None or ok > best[1]:
        best = (OFFS, ok, tot)
    print(f"OFFS={OFFS}: {ok}/{tot}")
OFFS, ok, tot = best
print(f"align OFFS={OFFS}: out {ok}/{tot}")
gold = run_gold(OFFS)

# phase identity
bad_ph = [(c, rtl[c]['k1'], (c - OFFS) % (4 * D))
          for c in range(CEND) if (c - OFFS) % (4 * D) != rtl[c]['k1']]
print(f"phase identity mismatches: {len(bad_ph)}", bad_ph[:5])

# output diffs
nbad = 0
for s, rec in enumerate(gold):
    c = rec['c']
    r = tout[c]
    if r[0] is None:
        continue
    if r != rec['ret']:
        nbad += 1
        if nbad <= 10:
            print(f"OUT c={c} s={s} k={rec['k']}: rtl={r} gold={rec['ret']} "
                  f"d0={rec['d0']} d1={rec['d1']}")
print(f"total output diffs: {nbad}")

# dram/dline read diffs
nd = 0
for s, rec in enumerate(gold):
    c = rec['c']
    if rec['d0'] is None:
        continue
    dr = rtl[c]['dr']; dl = rtl[c]['dl']
    drv = (rnum(dr[0]), rnum(dr[1]))
    dlv = (rnum(dl[0]), rnum(dl[1]))
    if drv[0] is None:
        continue
    rv = (s16(drv[0]), s16(drv[1]))
    lv = (s16(dlv[0]), s16(dlv[1]))
    if rv != rec['d0'] or lv != rec['d1']:
        nd += 1
        if nd <= 10:
            print(f"MEM c={c} s={s} k={rec['k']}: rtl dram={rv} dline={lv} "
                  f"gold d0={rec['d0']} d1={rec['d1']}")
print(f"total dram/dline read diffs: {nd}")
