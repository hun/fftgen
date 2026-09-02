"""SSR R=2 bring-up: fft_ssr_r23 vs SSRGoldenModel(arch="r23").

The r23 lanes natively emit bit-reversed (DIF), so the SSR runs the P8
corner order (native -> bitreversed) with fft_cross EMIT_BREV=1 and NO
lane reorder buffers. Lane = fft_sdf_r23(M = N/2); the golden lane is
R23ChainGoldenModel (the bit-exact shipped-decomposition chain).

env: N (default 1024, lane M = N/2 must be a supported r23 size),
     INV, NBLK (frames), SEED
"""
import os, subprocess, sys, random

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from config import FFTConfig
from golden_ssr import SSRGoldenModel
from twiddles import canonical_twiddles

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build_ssr23")
N = int(os.environ.get("N", "1024"))
INVERSE = bool(int(os.environ.get("INV", "0")))
NFRAMES = int(os.environ.get("NBLK", "4"))
SEED = int(os.environ.get("SEED", "7"))
R, M = 2, N // 2
TW, TD = 18, 17
NSTAGES_M = M.bit_length() - 1

os.makedirs(BUILD, exist_ok=True)
tw = canonical_twiddles(N, TW, TD, INVERSE)   # the crossbar's WN ROM
# the LANES are M-point r23 cores: their ROMs bake from the M-point
# table (W_M = W_N^2 at R=2 -- using the N table halves the rotation
# rate and corrupts every non-trivial lane product)
twm = canonical_twiddles(M, TW, TD, INVERSE)
mask = (1 << TW) - 1

# ---- lane r23 artifacts (M-point): 3 triple ROMs + concatenated leftover --
# NTRIP/NPAIRL as the RTL derives them
ntrip = 0
for t in (3, 2, 1):
    if NSTAGES_M >= 3 * t and (NSTAGES_M - 3 * t) % 2 == 0 and (M >> (3 * t)) >= 8:
        ntrip = t
        break
if ntrip == 0:
    print(f"N={N}: lane M={M} is not a supported r23 size")
    sys.exit(3)
npairl = (NSTAGES_M - 3 * ntrip) // 2
print(f"N={N} R=2: lane M={M} NSTAGES={NSTAGES_M} NTRIP={ntrip} NPAIRL={npairl}")

for m_ in range(3):
    g = M >> (3 * m_ + 3)
    layout = [(0, 2), (g, 6), (3 * g, 1), (4 * g, 5),
              (5 * g, 3), (6 * g, 7), (7 * g, 4)]
    words = [0] * (8 * g)
    for b, k in layout:
        for gg in range(g):
            re, im = twm[(k * gg * (1 << (3 * m_))) % M]
            words[b + gg] = ((re & mask) << TW) | (im & mask)
    with open(os.path.join(BUILD, f"fft_tw_r23_t{m_}.mem"), "w") as f:
        for w in words:
            f.write("%05x\n" % (w & ((1 << (2 * TW)) - 1)))

ds = [M >> (3 * ntrip + 2 * jj + 2) for jj in range(npairl)]
strides = [1 << (3 * ntrip + 2 * jj) for jj in range(npairl)]
lwords = [0] * sum(3 * d for d in ds)
i = 0
for jj in range(npairl):
    for which in (1, 2, 3):
        for g in range(ds[jj]):
            re, im = twm[(which * g * strides[jj]) % M]
            lwords[i] = ((re & mask) << TW) | (im & mask)
            i += 1
with open(os.path.join(BUILD, "fft_tw_r22_l.mem"), "w") as f:
    for w in lwords:
        f.write("%05x\n" % (w & ((1 << (2 * TW)) - 1)))

# ---- crossbar WN ROM: R*M words, row r = W_N^{r*p} (write_wn_mem layout) --
with open(os.path.join(BUILD, "fft_wn.mem"), "w") as f:
    for r in range(R):
        for p in range(M):
            re, im = tw[(r * p) % N]
            f.write("%05x\n" % (((re & mask) << TW) | (im & mask)))

# ---- stimulus --------------------------------------------------------------
rng = random.Random(SEED)
hi = 1 << 15
T = N * NFRAMES
samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1)) for _ in range(T)]
markers = [(1 if i % N == 0 else 0, 1 if i % N == N - 1 else 0)
           for i in range(T)]
TCLOCKS = T // R + 600
with open(os.path.join(BUILD, "stim_ssr.mem"), "w") as f:
    for c in range(TCLOCKS):
        w = []
        for r in range(R):
            idx = c * R + r
            re, im = samples[idx] if idx < T else (0, 0)
            u, l = markers[idx] if idx < T else (0, 0)
            w.append((re, im, u, l))
        packed = 0
        for r_, (re, im, u, l) in enumerate(w):
            packed |= (re & 0xFFFF) << (16 * r_)
            packed |= (im & 0xFFFF) << (16 * R + 16 * r_)
        packed |= (w[0][2] & 1) << (32 * R)
        packed |= (w[R - 1][3] & 1) << (32 * R + 1)
        f.write("%017x\n" % packed)

# ---- golden ----------------------------------------------------------------
cfg = FFTConfig(num_points=N, ssr=R, inverse=INVERSE,
                output_order="bitreversed", stage_mode="r23")
gm = SSRGoldenModel(cfg, arch="r23")
gouts = gm.process_stream(samples, markers)
gframes = []
cur = None
for (re, im, u, l) in gouts:
    if u: cur = []
    if cur is not None: cur.append((re, im, l))
    if l and cur is not None: gframes.append(cur); cur = None

# ---- RTL --------------------------------------------------------------------
pack_m = sum(1 << (2 * s) for s in range(NSTAGES_M))
pack_m = sum(1 << (2 * s) for s in range(NSTAGES_M))
r = subprocess.run(
    ["iverilog", "-g2012", "-o", "ssr23.vvp",
     "-Ptb_ssr23.INV=%d" % int(INVERSE),
     "-Ptb_ssr23.NUM_POINTS=%d" % N,
     "-Ptb_ssr23.PACK=%d" % pack_m,
     os.path.join(ROOT, "rtl", "fft_stage_r23.v"),
     os.path.join(ROOT, "rtl", "fft_stage_r22.v"),
     os.path.join(ROOT, "rtl", "fft_sdf_r23.v"),
     os.path.join(ROOT, "rtl", "fft_cross.v"),
     os.path.join(ROOT, "rtl", "fft_ssr_r23.v"),
     os.path.join(HERE, "tb_ssr23.v")],
    cwd=BUILD, capture_output=True, text=True)
if r.returncode:
    print(r.stdout + r.stderr); sys.exit(1)
subprocess.run(["vvp", "ssr23.vvp"], cwd=BUILD, capture_output=True, text=True)

def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v

rtl = []
for ln in open(os.path.join(BUILD, "out_ssr.hex")):
    p = ln.split()
    if 'x' in ln:
        rtl.append(None); continue
    word = [(s16(int(p[2 * q], 16)), s16(int(p[2 * q + 1], 16))) for q in range(R)]
    rtl.append((word, int(p[2 * R]), int(p[2 * R + 1])))

# frame-aligned comparison (complete tuser..tlast windows in both streams)
def frames_of(stream):
    # expand clock entries (R bins each) to one entry per bin; the
    # markers are shared per clock (tuser on lane 0, tlast on lane R-1)
    fr = []; cur = None
    for item in stream:
        if item is None:
            continue
        word, u, l = item
        for q in range(R):
            if u and q == 0: cur = []
            if cur is not None: cur.append((word[q], u if q == 0 else 0,
                                            l if q == R - 1 else 0))
            if l and q == R - 1 and cur is not None:
                fr.append(cur); cur = None
    return fr

rframes = [fr for fr in frames_of(rtl) if len(fr) == N]
gframes = [fr for fr in gframes if len(fr) == N]
print("frames: golden", len(gframes), "rtl", len(rframes))
# align from the END: the last complete frame = the last input frame in
# both streams (the golden drops fill frames, the RTL does not)
nf = min(len(gframes), len(rframes))
if nf == 0:
    print("NO complete frames -- check marker/latency plumbing"); sys.exit(2)
# frame-index alignment search: the golden drops fill frames, the RTL
# does not -- find the offset with the most matching bins
def frame_diff(go, ro, off):
    worst = 0; bad = 0; tot = 0
    for f in range(len(go)):
        rf = f + off
        if rf < 0 or rf >= len(ro): continue
        for e in range(N):
            gw = go[f][e]; rw = ro[rf][e]
            d = max(abs(gw[0] - rw[0][0]), abs(gw[1] - rw[0][1]))
            tot += 1; worst = max(worst, d)
            if d > 4: bad += 1
    return bad, worst, tot

best = None
for off in range(-(len(rframes) - 1), len(rframes)):
    bad, worst, tot = frame_diff(gframes, rframes, off)
    if tot == 0: continue
    if best is None or (bad, -tot) < (best[0], -best[2]): best = (bad, worst, tot, off)
if best is None:
    print("no frame overlap"); sys.exit(2)
bad, worst, tot, off = best
print(f"frame offset {off}: {tot - bad}/{tot} bins within 4 LSB, worst {worst}")
nf = min(len(gframes), len(rframes) - abs(off))
gframes = gframes[ max(0, -off): ] if off >= 0 else gframes[ :nf]
rframes = rframes[ off: ] if off >= 0 else rframes[ max(0, -off): ]
nf = min(len(gframes), len(rframes))
gframes = gframes[:nf]; rframes = rframes[:nf]
tol = R // 2 + 1 + 2          # SSR documented tolerance + small slack
worst = 0; bad = 0; tot = 0
badlist = []
for f in range(nf):
    for e in range(N):
        gw = gframes[f][e]
        rw = rframes[f][e]
        d = max(abs(gw[0] - rw[0][0]), abs(gw[1] - rw[0][1]))
        tot += 1
        worst = max(worst, d)
        if d > tol:
            bad += 1
            if len(badlist) < 6:
                badlist.append((f, e, rw[0], (gw[0], gw[1]), d))
print(f"compared {tot} bins: worst delta {worst} (tol {tol}), bad {bad}")
if bad == 0:
    print(f"SSR R=2 r23 (N={N}, INV={int(INVERSE)}) MATCHES GOLDEN "
          f"({nf} frames, {nf*N} bins)")
else:
    for row in badlist: print("  ", row)
