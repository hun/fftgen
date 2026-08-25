"""Verilator artifact generation + bit-exact verification for the FFT core.

Follows the firgen pattern: generate stimulus/expected from the golden
model, compile the RTL with Verilator (-G parameter overrides), run, and
diff actual vs expected.

P2 scope: R=1, native->bitreversed (DIF), forward/inverse, markers,
freeze masks.
"""

import os
import shutil
import subprocess
import sys
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from config import FFTConfig
from golden import OrderedFFTModel
from stimuli import freeze_mask, random_frame


def _hex(v: int, width: int) -> str:
    return format(v & ((1 << width) - 1), "0%dx" % ((width + 3) // 4))


def _hex_wide(v: int) -> str:
    return format(v & 0xFFFFFFFFFFFFFFFF, "016x")


def write_twiddle_mem(cfg: FFTConfig, path: str) -> None:
    """Twiddle ROM contents: N words, {re, im} packed MSB:LSB, stage s at
    [BASE_s .. BASE_s + D_s - 1] (the generator layout fft_sdf.v expects)."""
    from twiddles import canonical_twiddles
    N = cfg.num_points
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    words = []
    dit = cfg.is_dit
    for s in range(cfg.num_stages):
        if dit:
            D = 1 << s
            idxs = [(j << (cfg.num_stages - s - 1)) % N for j in range(D)]
        else:
            D = N >> (s + 1)
            idxs = [(i << s) % N for i in range(D)]
        for k in idxs:
            re, im = tw[k]
            packed = ((re & ((1 << cfg.twiddle_width) - 1)) << cfg.twiddle_width) \
                     | (im & ((1 << cfg.twiddle_width) - 1))
            words.append(packed)
    while len(words) < N:
        words.append(0)
    with open(path, "w") as f:
        for w in words:
            f.write(_hex(w, cfg.twiddle_width * 2) + "\n")


def write_lane_twiddle_mem(cfg_m: FFTConfig, path: str) -> None:
    """Lane twiddle ROM for SSR: plain M-point layout (stage bases are
    irrelevant to the lane engines' ROM_BASE offsets -- reuse the R=1
    generator layout)."""
    write_twiddle_mem(cfg_m, path)


def write_wn_mem(cfg: FFTConfig, path: str) -> None:
    """Crossbar pre-twiddle ROM for fft_cross.v:
    R*M words, row r = 0..R-1 holds W_N^{r*p} for p in [0,M)."""
    from twiddles import canonical_twiddles
    N, R = cfg.num_points, cfg.ssr
    M = N // R
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    with open(path, "w") as f:
        for r in range(R):
            for p in range(M):
                re, im = tw[(r * p) % N]
                packed = ((re & ((1 << cfg.twiddle_width) - 1))
                          << cfg.twiddle_width) \
                         | (im & ((1 << cfg.twiddle_width) - 1))
                f.write(_hex(packed, cfg.twiddle_width * 2) + "\n")


def generate_ssr(cfg: FFTConfig, outdir: str, num_frames: int = 4,
                 seed: int = 1, quiet: bool = True,
                 pad_frames: int = None) -> dict:
    """SSR build: R lanes of M-point fft_top + fft_cross. v1 contract:
    native input, native (block-contiguous) output.

    Pipeline fill consumes ~latency/M input frames before the first
    complete output frame emerges; `pad_frames` (default: enough to
    cover fill) filler frames are PREPENDED so the last `num_frames`
    input frames emerge fully. The comparison locates the frame offset
    by search and verifies the overlapping tail."""
    import shutil
    os.makedirs(outdir, exist_ok=True)
    N, R = cfg.num_points, cfg.ssr
    assert cfg.input_order == "native" and cfg.output_order == "native"
    M = N // R

    from golden_ssr import SSRGoldenModel as _P
    if pad_frames is None:
        pad_frames = (_P(cfg).latency + M - 1) // M + 2

    rng = __import__("random").Random(seed)
    frames = [[(rng.randint(-2 ** (cfg.sample_width - 1),
                              2 ** (cfg.sample_width - 1) - 1),
                rng.randint(-2 ** (cfg.sample_width - 1),
                            2 ** (cfg.sample_width - 1) - 1))
               for _ in range(N)]
              for _ in range(num_frames + pad_frames)]
    samples = [s for fr in frames for s in fr]
    markers = []
    for f in range(num_frames + pad_frames):
        markers += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]

    from golden_ssr import SSRGoldenModel
    m = SSRGoldenModel(cfg)

    # stimulus: flat natural samples, hex
    with open(os.path.join(outdir, "stimulus.txt"), "w") as f:
        for (re_, im_), (u, l) in zip(samples, markers):
            f.write(f"{_hex(re_, cfg.sample_width)} "
                    f"{_hex(im_, cfg.sample_width)} {u} {l}\n")

    # expected: flat emission stream (R lines per output word)
    got = m.process_stream(samples, markers=markers)
    with open(os.path.join(outdir, "expected.txt"), "w") as f:
        for re_, im_, u, l in got:
            f.write(f"{re_} {im_} {u} {l}\n")
        n_expected = len(got)

    # RTL sources
    rtl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "rtl")
    for fn in ("fft_sdf.v", "fft_reorder.v", "fft_top.v",
               "fft_cross.v", "fft_ssr.v"):
        shutil.copy(os.path.join(rtl_dir, fn), outdir)

    # lane config artifacts: twiddle mem + preload pack + scaling pack
    import dataclasses
    lane_cfg = dataclasses.replace(cfg, num_points=M, ssr=1,
                                   input_order="native",
                                   output_order="bitreversed")
    write_lane_twiddle_mem(lane_cfg, os.path.join(outdir,
                                                  "fft_twiddles_lane.mem"))
    write_wn_mem(cfg, os.path.join(outdir, "fft_wn.mem"))

    intern_m = (cfg.sample_width
                + max(0, lane_cfg.num_stages - sum(lane_cfg.shifts)) + 1)
    pack_m = 0
    for s_, sh in enumerate(lane_cfg.shifts):
        pack_m |= (sh & 3) << (2 * s_)
    from golden import SDFGoldenModel
    SDFGoldenModel(dataclasses.replace(
        lane_cfg, input_order="native", output_order="bitreversed"), dit=False)
    # identical field layout to the R=1 path (fft_sdf.v slices it there):
    # per stage {wptr16, pwp16, raddr16, pipe6, phase_i8, compute1} = 63b
    _gm = SSRGoldenModel(cfg) if False else None
    lane_full = dataclasses.replace(cfg, num_points=M, ssr=1,
                                    input_order="native",
                                    output_order="bitreversed")
    _gm = SDFGoldenModel(lane_full, dit=False)
    pl_pack = 0
    for i, pl in enumerate(_gm.stage_preloads):
        pipe_bits = int("".join("1" if b else "0"
                                for b in reversed(pl["pipe"][:6])), 2) & 0x3F
        stage = ((pl["wptr"] & 0xFFFF)
                 | ((pl["pwp"] & 0xFFFF) << 16)
                 | ((pl["raddr"] & 0xFFFF) << 32)
                 | (pipe_bits << 48)
                 | ((pl["phase_i"] & 0xFF) << 54)
                 | ((1 if pl["compute"] else 0) << 62))
        pl_pack |= stage << (63 * i)
    with open(os.path.join(outdir, "fft_preloads.vh"), "w") as f:
        f.write("`define FFTGEN_PRELOAD_PACK 512'h%0128x\n" % pl_pack)

    tb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "tb", "tb_fft_ssr.cpp")
    gargs = [
        "+define+FFTGEN_PRELOADS",
        "+define+TB_SSR=%d" % R,
        "+incdir+.",
        f"-GNUM_POINTS={N}",
        f"-GSSR={R}",
        f"-GSAMPLE_WIDTH={cfg.sample_width}",
        f"-GSAMPLE_DECIMAL={cfg.sample_decimal}",
        f"-GOUTPUT_WIDTH={cfg.output_width}",
        f"-GOUTPUT_DECIMAL={cfg.output_decimal}",
        f"-GTWIDDLE_WIDTH={cfg.twiddle_width}",
        f"-GTWIDDLE_DECIMAL={cfg.twiddle_decimal}",
        f"-GSCALING_PACK=32'h{pack_m:08x}",
        f"-GINTERN_WIDTH={intern_m}",
        "-GPIPE_DEPTH=7",
    ]
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", "fft_ssr", "-Wno-fatal",
           "-CFLAGS", f"-DTB_SAMPLE_WIDTH={cfg.sample_width} "
                      f"-DTB_OUTPUT_WIDTH={cfg.output_width} "
                      f"-DTB_SSR={R}",
           *gargs,
           "fft_ssr.v", "fft_top.v", "fft_sdf.v", "fft_reorder.v",
           "fft_cross.v", tb]
    r = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-2000:], "outdir": outdir}

    r = subprocess.run([os.path.join("obj_dir", "Vfft_ssr")],
                       cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-2000:], "outdir": outdir}

    with open(os.path.join(outdir, "expected.txt")) as f:
        exp = [tuple(int(x) for x in ln.split()) for ln in f if ln.strip()]
    with open(os.path.join(outdir, "actual.txt")) as f:
        act = [tuple(int(x) for x in ln.split()) for ln in f if ln.strip()]
    tol = R // 2 + 1

    def samp_close(e, a):
        # values compared with tolerance; markers verified separately
        # below (fill-skip shifts them uniformly)
        return (all(abs(x - y) <= tol for x, y in zip(e[:2], a[:2])))


    # The RTL emits some leading pipeline-fill words before its stream
    # locks to the frame grid; find the word offset at which the whole
    # remaining RTL stream matches the head of expected, then verify.
    def vals_ok(e, a):
        return all(abs(x - y) <= tol for x, y in zip(e[:2], a[:2]))
    d0 = None
    n_act = len(act)
    for skip_w in range(0, (len(act) // R)):
        base = skip_w * R
        n_cmp = min(len(exp), n_act - base)
        if n_cmp < N:
            break
        if all(vals_ok(exp[i], act[base + i]) for i in range(n_cmp)):
            d0 = base
            break
    if d0 is None:
        return {"rc": 1, "outdir": outdir,
                "log": "no word-offset alignment found",
                "first_bad": (0, act[0], exp[0])}
    tail_a = act[d0:]
    n_cmp = min(len(exp), len(tail_a))
    mism = [i for i in range(n_cmp) if not samp_close(exp[i], tail_a[i])]
    # markers: after alignment, every N-sample group must contain exactly
    # one SOF (first line) and one EOF pattern consistent with frames
    mk_bad = 0
    for b in range(n_cmp // N):
        seg = [act[base + b * N + e] for e in range(N)]
        us = sum(1 for x in seg if x[2] == 1)
        ls = sum(1 for x in seg if x[3] == 1)
        if us != 1 or ls != 1:
            mk_bad += 1
    # require at least one full frame of overlapping verified samples
    ok = (n_cmp >= N) and not mism and mk_bad == 0
    if mk_bad:
        mism.append(("markers", mk_bad))
    first_bad = None
    if mism:
        i = mism[0]
        first_bad = (d0 + i, tail_a[i], exp[i])
    return {"rc": 0 if ok else 1, "outdir": outdir,
            "n_expected": len(exp), "n_actual": len(act),
            "offset": d0, "first_bad": first_bad}


def _markers(N: int, num_frames: int) -> List[Tuple[int, int]]:
    out = []
    for _ in range(num_frames):
        for j in range(N):
            out.append((1 if j == 0 else 0, 1 if j == N - 1 else 0))
    return out


def generate(cfg: FFTConfig, outdir: str, num_frames: int = 4,
             seed: int = 1, freeze: Optional[str] = None,
             quiet: bool = True) -> dict:
    """Write artifacts + build + run + compare. Returns {'rc', 'outdir', ...}."""
    os.makedirs(outdir, exist_ok=True)
    N = cfg.num_points
    rng = __import__("random").Random(seed)
    frames = [random_frame(N, cfg.sample_width, rng) for _ in range(num_frames)]
    samples = [s for fr in frames for s in fr]

    # DIT consumes bit-reversed input order: reorder BEFORE the expected
    # is computed (expected must see the same stream the core consumes)
    if cfg.is_dit:
        br = [int(format(k, f"0{cfg.num_stages}b")[::-1], 2)
              for k in range(N)]
        samples = [fr[br[j]] for fr in frames for j in range(N)]

    # golden expected (OrderedFFTModel covers every order corner)
    m = OrderedFFTModel(cfg)
    markers = _markers(N, num_frames)
    expected = m.process_stream(samples, markers=markers)

    # stimulus + mask
    with open(os.path.join(outdir, "stimulus.txt"), "w") as f:
        for (re, im), (u, l) in zip(samples, markers):
            f.write(f"{_hex(re, cfg.sample_width)} {_hex(im, cfg.sample_width)} "
                    f"{u} {l}\n")
    if freeze is not None:
        mask = freeze_mask(len(samples) * 8, seed=seed, style=freeze)
        with open(os.path.join(outdir, "mask.txt"), "w") as f:
            for e in mask:
                f.write("1\n" if e else "0\n")
    with open(os.path.join(outdir, "expected.txt"), "w") as f:
        for re, im, u, l in expected:
            f.write(f"{re} {im} {u} {l}\n")

    # RTL + twiddle ROM
    rtl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "rtl")
    for fn in ("fft_sdf.v", "fft_reorder.v", "fft_top.v"):
        shutil.copy(os.path.join(rtl_dir, fn), outdir)
    write_twiddle_mem(cfg, os.path.join(outdir, "fft_twiddles.mem"))

    # build with verilator
    tb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "tb", "tb_fft_sdf.cpp")
    intern = cfg.sample_width + max(0, cfg.num_stages - sum(cfg.shifts)) + 1
    # SCALING_PACK: 2 bits per stage
    pack = 0
    for s, sh in enumerate(cfg.shifts):
        pack |= (sh & 3) << (2 * s)
    # reorder needed when the outer output order differs from the core's
    # natural output order (bitreversed for DIF, native for DIT)
    core_out = "bitreversed" if not cfg.is_dit else "native"
    reorder_out = (cfg.output_order != core_out)
    # per-stage post-warm reset preloads (packed for the RTL generate).
    # Stage timing is independent of I/O ordering: build the model with the
    # core's natural orders so order-conversion configs don't trip its guard.
    import dataclasses
    from golden import SDFGoldenModel
    if cfg.is_dit:
        nat_in, nat_out = "bitreversed", "native"
    else:
        nat_in, nat_out = "native", "bitreversed"
    _gm = SDFGoldenModel(
        dataclasses.replace(cfg, input_order=nat_in, output_order=nat_out),
        dit=cfg.is_dit)
    pl_pack = 0
    for i, pl in enumerate(_gm.stage_preloads):
        pipe_bits = int("".join("1" if b else "0"
                                for b in reversed(pl["pipe"][:6])), 2) & 0x3F
        stage = (pl["wptr"] & 0xFFFF) | ((pl["pwp"] & 0xFFFF) << 16) \
                | ((pl["raddr"] & 0xFFFF) << 32) \
                | (pipe_bits << 48) \
                | ((pl["phase_i"] & 0xFF) << 54) \
                | ((1 if pl["compute"] else 0) << 62)
        pl_pack |= stage << (63 * i)
    # per-stage preloads travel via a generated header (the -G parser
    # caps parameter values at 32 bits)
    with open(os.path.join(outdir, "fft_preloads.vh"), "w") as f:
        f.write("`define FFTGEN_PRELOAD_PACK 512'h%0128x\n" % pl_pack)
    gargs = [
        "+define+FFTGEN_PRELOADS",
        "+incdir+.",
        f"-GNUM_POINTS={cfg.num_points}",
        f"-GSAMPLE_WIDTH={cfg.sample_width}",
        f"-GSAMPLE_DECIMAL={cfg.sample_decimal}",
        f"-GOUTPUT_WIDTH={cfg.output_width}",
        f"-GOUTPUT_DECIMAL={cfg.output_decimal}",
        f"-GTWIDDLE_WIDTH={cfg.twiddle_width}",
        f"-GTWIDDLE_DECIMAL={cfg.twiddle_decimal}",
        f"-GSCALING_PACK=32'h{pack:08x}",
        f"-GINTERN_WIDTH={intern}",
        f"-GTOPOLOGY={'1' if cfg.is_dit else '0'}",
        f"-GREORDER_OUT={'1' if reorder_out else '0'}",
    ]
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", "fft_top", "-Wno-fatal",
           "-CFLAGS", f"-DTB_SAMPLE_WIDTH={cfg.sample_width} "
                      f"-DTB_OUTPUT_WIDTH={cfg.output_width}",
           *gargs,
           "fft_top.v", "fft_sdf.v", "fft_reorder.v", tb]
    r = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-2000:], "outdir": outdir}

    r = subprocess.run([os.path.join("obj_dir", "Vfft_top")],
                       cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-2000:], "outdir": outdir}

    # compare
    with open(os.path.join(outdir, "expected.txt")) as f:
        exp = [tuple(int(x) for x in ln.split()) for ln in f if ln.strip()]
    with open(os.path.join(outdir, "actual.txt")) as f:
        act = [tuple(int(x) for x in ln.split()) for ln in f if ln.strip()]
    ok = (len(exp) == len(act)) and all(a == b for a, b in zip(exp, act))
    first_bad = next(((i, a, b) for i, (a, b) in enumerate(zip(act, exp))
                      if a != b), None)
    return {"rc": 0 if ok else 1,
            "outdir": outdir,
            "n_expected": len(exp),
            "n_actual": len(act),
            "first_bad": first_bad,
            "log": ""}


def verify_verilator(cfg: FFTConfig, outdir: str, **kw) -> dict:
    return generate(cfg, outdir, **kw)


if __name__ == "__main__":
    cfg = FFTConfig(num_points=8)
    res = generate(cfg, "build/p2_demo")
    print(res)
