"""Debug aid: random stimulus, record the golden's products (operand +
twiddle index), run the RTL TB, and for each mismatch brute-force which
twiddle index (class k, group offset d) explains the RTL's output."""
import os, sys, random, subprocess
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles
from golden import _R23DIFStage, complex_multiply_karatsuba, round_shift

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
G, W, TW, TD = 128, 16, 18, 17
N = 8 * G
LAT = 7 * G + 2
NBLK = int(os.environ.get("NBLK", "2"))
T = N * NBLK
INVERSE = False

tw = canonical_twiddles(N, TW, TD, INVERSE)
t2idx = {}
for i, t in enumerate(tw):
    t2idx.setdefault(t, i)

prods = []
orig_prod = _R23DIFStage._prod
def rec_prod(self, z, t, sh):
    prods.append((self._pos, z, t2idx.get(t, -1), sh))
    return orig_prod(self, z, t, sh)
_R23DIFStage._prod = rec_prod
orig_step = _R23DIFStage.step
def rec_step(self, x, pos):
    self._pos = pos
    return orig_step(self, x, pos)
_R23DIFStage.step = rec_step

rng = random.Random(20260214)
hi = 1 << (W - 1)
samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
           for _ in range(T)]

with open(os.path.join(BUILD, "stim.mem"), "w") as f:
    for pos in range(T + LAT + 4):
        re, im = samples[pos] if pos < T else (0, 0)
        f.write("%08x\n" % (((im & 0xFFFF) << 16) | (re & 0xFFFF)))

st = _R23DIFStage(0, N, 1, 1, 1, TD, tw, INVERSE)
outs = []
for pos in range(T + LAT + 4):
    src = samples[pos] if pos < T else (0, 0)
    outs.append(st.step(src, pos))

r = subprocess.run(["vvp", "tb.vvp"], cwd=BUILD, capture_output=True, text=True)
with open(os.path.join(BUILD, "out.hex")) as f:
    rtl = []
    for ln in f:
        ln = ln.strip()
        rtl.append(None if 'x' in ln else tuple(int(x, 16) for x in ln.split()))

H = int(os.environ.get("H", "8"))

def cshift(z, idx, sh):
    pr, pi = complex_multiply_karatsuba(z[0], z[1],
                                        tw[idx][0], tw[idx][1])
    return (round_shift(pr, sh), round_shift(pi, sh))

def s16(v):
    return v if v < 0x8000 else v - 0x10000

prod_by_val = {}
for (step, z, idx, sh) in prods:
    v = cshift(z, idx, sh)
    prod_by_val.setdefault((s16(v[0] & 0xFFFF), s16(v[1] & 0xFFFF)),
                           []).append((step, z, idx, sh))

from collections import Counter
nbad = nok = 0
expl = Counter(); noexpl = 0
first = []
for c in range(len(rtl)):
    p = c - H
    if p < 0 or p >= len(outs) or rtl[c] is None:
        continue
    g = outs[p]
    a = rtl[c]
    if (g[0] & 0xFFFF) == a[0] and (g[1] & 0xFFFF) == a[1]:
        nok += 1
        continue
    nbad += 1
    key = (s16(g[0]), s16(g[1]))
    cands = prod_by_val.get(key)
    if not cands:
        continue          # y0 (no product) or value collision
    step, z, idx, sh = cands[0]
    gpos = (p % (8 * G)) % G
    found = None
    for k in range(8):
        for d in range(-4, 5):
            i2 = (k * ((gpos + d) % G)) % N
            if tuple(s16(v & 0xFFFF) for v in cshift(z, i2, sh)) == \
                    (s16(a[0]), s16(a[1])):
                found = (k, d, i2)
                break
        if found:
            break
    if found:
        expl[(found[0], found[1], idx // G)] += 1
    else:
        noexpl += 1
    if len(first) < 10:
        first.append((p, gpos, idx, idx // G if idx >= 0 else -1, z, sh,
                      a, g, found))
print(f"nok={nok} nbad={nbad} explained={sum(expl.values())} "
      f"unexplained={noexpl}")
for f_ in first:
    print(f"pos={f_[0]} g={f_[1]} gold_slot={f_[3]} idx={f_[2]} sh={f_[5]}")
    print(f"   z={f_[4]}")
    print(f"   rtl={f_[6]} gold={f_[7]} explains(k,d,idx)={f_[8]}")
print("explanation census (k, d, gold_slot):", expl.most_common(10))
