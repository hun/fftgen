"""P7 step 1: verify the PRODUCTION r22 core (generic rtl/fft_top_r22.v
+ rtl/fft_sdf_r22.v, parameters via -G) bit-exactly against
R22SDFGoldenModel -- the same contract the spike top (top_gen.py)
proved, now through the self-contained RTL (INVERSE param, K_PRELOAD
re-alignment, leftover parity preloads, marker/LATENCY window).

Run:  python3 spikes/S5_r22/rtl_check_prod.py
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import FFTConfig
from golden import R22SDFGoldenModel
from rtl_check import write_r22_twiddle_mem

SPIKE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(SPIKE, "..", ".."))
RTL = os.path.join(ROOT, "rtl")
TB = os.path.join(ROOT, "tb")


def _hex(v, width):
    return format(v & ((1 << width) - 1), "0%dx" % ((width + 3) // 4))


def run(cfg, num_frames=2, seed=7, freeze=None):
    import random
    N = cfg.num_points
    outdir = os.path.join(SPIKE, "build_prod")
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    for fn in ("fft_top_r22.v", "fft_sdf_r22.v", "fft_stage_r22.v",
               "fft_sdf.v", "fft_reorder.v"):
        shutil.copy(os.path.join(RTL, fn), outdir)
    shutil.copy(os.path.join(TB, "tb_fft_r22.cpp"), outdir)
    # fft_top_r22's default TWIDDLE_FILE is the lane name; match it
    write_r22_twiddle_mem(cfg, os.path.join(outdir,
                            "fft_twiddles_r22_lane.mem"))

    rng = random.Random(seed)
    hi = 2 ** (cfg.sample_width - 1)
    samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
               for _ in range(num_frames * N)]
    markers = [(1 if k % N == 0 else 0, 1 if k % N == N - 1 else 0)
               for k in range(num_frames * N)]
    with open(os.path.join(outdir, "stimulus.txt"), "w") as f:
        for (re, im), (u, l) in zip(samples, markers):
            f.write("%s %s %d %d\n" % (_hex(re, cfg.sample_width),
                                       _hex(im, cfg.sample_width), u, l))
    if freeze is not None:
        from stimuli import freeze_mask
        mask = freeze_mask(len(samples) * 8, seed=seed, style=freeze)
        with open(os.path.join(outdir, "mask.txt"), "w") as f:
            for e in mask:
                f.write("1\n" if e else "0\n")

    m = R22SDFGoldenModel(cfg)
    expected = m.process_stream(samples, markers=markers)
    with open(os.path.join(outdir, "expected.txt"), "w") as f:
        for re, im, u, l in expected:
            f.write("%d %d %d %d\n" % (re, im, u, l))

    intern = cfg.sample_width + max(0, cfg.num_stages - sum(cfg.shifts)) + 1
    pack = 0
    for s, sh in enumerate(cfg.shifts):
        pack |= (sh & 3) << (2 * s)
    gargs = [
        f"-GNUM_POINTS={cfg.num_points}",
        f"-GSAMPLE_WIDTH={cfg.sample_width}",
        f"-GSAMPLE_DECIMAL={cfg.sample_decimal}",
        f"-GOUTPUT_WIDTH={cfg.output_width}",
        f"-GOUTPUT_DECIMAL={cfg.output_decimal}",
        f"-GTWIDDLE_WIDTH={cfg.twiddle_width}",
        f"-GTWIDDLE_DECIMAL={cfg.twiddle_decimal}",
        f"-GSCALING_PACK=32'h{pack:08x}",
        f"-GINTERN_WIDTH={intern}",
        f"-GINVERSE={1 if cfg.inverse else 0}",
        "-GTOPOLOGY=0", "-GREORDER_OUT=0",
    ]
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", "fft_top_r22", "-Wno-fatal",
           "-CFLAGS", f"-DTB_SAMPLE_WIDTH={cfg.sample_width} "
                      f"-DTB_OUTPUT_WIDTH={cfg.output_width}",
           *gargs,
           "fft_top_r22.v", "fft_sdf_r22.v", "fft_stage_r22.v",
           "fft_sdf.v", "fft_reorder.v", "tb_fft_r22.cpp"]
    r = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-3000:]}
    r = subprocess.run([os.path.join("obj_dir", "Vfft_top_r22")],
                       cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-3000:]}

    act = [tuple(int(x) for x in ln.split())
           for ln in open(os.path.join(outdir, "actual.txt")) if ln.strip()]
    exp = [tuple(int(x) for x in ln.split())
           for ln in open(os.path.join(outdir, "expected.txt")) if ln.strip()]
    if len(act) != len(exp):
        return {"rc": 1, "log": f"count {len(act)} vs {len(exp)}",
                "head_act": act[:4], "head_exp": exp[:4]}
    bad = next(((i, a, b) for i, (a, b) in enumerate(zip(act, exp))
                if a != b), None)
    if bad:
        return {"rc": 1, "log": "value/marker mismatch", "first_bad": bad}
    return {"rc": 0, "n": len(exp)}


if __name__ == "__main__":
    fails = 0
    cfgs = []
    for N in (2, 4, 8, 16, 32, 64, 128, 256):
        for inv in (False, True):
            cfgs.append((f"N={N} inv={int(inv)}", FFTConfig(num_points=N,
                                                             inverse=inv)))
    cfgs += [
        ("w8/tw8", FFTConfig(num_points=16, sample_width=8, twiddle_width=8)),
        ("w25/o20", FFTConfig(num_points=16, sample_width=25, output_width=20)),
        ("12.3->20.2", FFTConfig(num_points=16, sample_width=12,
                                 output_width=20, sample_decimal=3,
                                 output_decimal=2)),
        ("tw10.8", FFTConfig(num_points=16, twiddle_width=10,
                             twiddle_decimal=8)),
        ("scal0", FFTConfig(num_points=8, scaling=(0, 0, 0), output_width=24)),
        ("scal201", FFTConfig(num_points=8, scaling=(2, 0, 1),
                              output_width=24)),
        ("scal222", FFTConfig(num_points=8, scaling=(2, 2, 2),
                              output_width=24)),
    ]
    for name, cfg in cfgs:
        res = run(cfg)
        if res["rc"] == 0:
            print(f"{name:16s}: BIT-EXACT ({res['n']} samples)")
        else:
            fails += 1
            print(f"{name:16s}: FAIL {res.get('log','')[:300]}")
            if "first_bad" in res:
                print("   first_bad:", res["first_bad"])
            if "head_act" in res:
                print("   act:", res["head_act"], " exp:", res["head_exp"])
    for style in ("periodic", "bursty"):
        res = run(FFTConfig(num_points=16), freeze=style)
        ok = "BIT-EXACT" if res["rc"] == 0 else "FAIL"
        fails += res["rc"] != 0
        print(f"freeze-{style:10s}: {ok} {res.get('log','')[:200]}")
    sys.exit(1 if fails else 0)
