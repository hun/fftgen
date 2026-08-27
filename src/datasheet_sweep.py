#!/usr/bin/env python3
"""Datasheet sweep: KU5P OOC synthesis across N, R, and architecture (r2 vs r22).

For each (N, R, arch) this writes the generator artifacts (twiddle ROMs +
post-warm preloads where applicable), runs Vivado out-of-context synthesis,
and parses utilization + timing into a combined table with BOTH cores
side-by-side for easy comparison.

* ``r2``  -- plain radix-2 SDF (log2(N) stages, ``rtl/fft_sdf.v`` + ``fft_stage``).
          Supports all R (1,2,4,8). P6 trivial-stage reduction included.
* ``r22`` -- radix-2^2 SDF (P7, ``rtl/fft_stage_r22.v`` via spike top
          ``spikes/S5_r22/top_gen.py``). R=1: single-stream core;
          R=2 (P7, this impl): SSR via ``rtl/fft_ssr_r22.v`` (R × M-point
          ``fft_top_r22`` lanes + ``fft_cross``). One complex multiply per
          stage pair (3 products / 4-group), ~2× DSP savings.

Results are cached per config in ``<jobs-dir>/N*_R*_r22| r2/result.json``;
re-runs skip completed configs, so an interrupted sweep resumes.

Usage
-----
    # full sweep (both arches, all N×R)
    python3 -m src.datasheet_sweep -j 4

    # r2 only (backwards compat with spike sweep)
    python3 -m src.datasheet_sweep --arch r2 -j 4

    # r22 only, small N
    python3 -m src.datasheet_sweep --arch r22 --r1 64 256 1024 -j 2

    # explicit N lists
    python3 -m src.datasheet_sweep --r1 64 1024 --r2 256 --arch both -j 4

Tables are written to ``<jobs-dir>/datasheet.md`` (per-arch rows with
``Arch`` column) plus ``datasheet.csv`` and a side-by-side R=1 comparison
``comparison.md``. Legacy spike sweep output (``build/datasheet``) is
compatible -- this module defaults to ``build/datasheet`` as well but can
be pointed at ``spikes/S2_timing/build/datasheet`` via ``--jobs-dir``.

See ``doc/datasheet.md`` for the published table and ``doc/architecture.md
§10`` for the r2 vs r22 contract note (bit-identical vs re-pinned rounding).
"""

import argparse
import concurrent.futures
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
# spike top_gen for r22 -- keep local copy logic too, but reuse if present
SPIKE_R22 = os.path.join(ROOT, "spikes", "S5_r22")

VIVADO = os.environ.get("VIVADO_BIN", "/tools/Xilinx/2026.1/Vivado/bin/vivado")
PART = "xcku5p-ffva676-1-e"
CLK_NS = 2.0

# ---------------------------------------------------------------------------
# Vivado TCL templates
# ---------------------------------------------------------------------------

TCL_R2 = """\
set part    [lindex $argv 0]
set npts    [lindex $argv 1]
set ssr     [lindex $argv 2]
set pack    [lindex $argv 3]
set intern  [lindex $argv 4]

create_project -in_memory -part $part
if {$ssr > 1} {
    add_files -fileset sources_1 [list \\
        @RTL@/fft_ssr.v \\
        @RTL@/fft_top.v \\
        @RTL@/fft_sdf.v \\
        @RTL@/fft_reorder.v \\
        @RTL@/fft_cross.v ]
    set_property top fft_ssr [current_fileset]
    set wn_abs  [file normalize [file join [pwd] fft_wn.mem]]
    set tw_abs  [file normalize [file join [pwd] fft_twiddles_lane.mem]]
    synth_design -top fft_ssr \\
        -generic NUM_POINTS=$npts -generic SSR=$ssr \\
        -generic WN_FILE=$wn_abs -generic TWIDDLE_FILE=$tw_abs \\
        -generic SAMPLE_WIDTH=16 -generic SAMPLE_DECIMAL=0 \\
        -generic OUTPUT_WIDTH=16 -generic OUTPUT_DECIMAL=0 \\
        -generic TWIDDLE_WIDTH=18 -generic TWIDDLE_DECIMAL=17 \\
        -generic SCALING_PACK=$pack -generic INTERN_WIDTH=$intern \\
        -generic PIPE_DEPTH=10 -generic INVERSE=0
} else {
    add_files -fileset sources_1 [list \\
        @RTL@/fft_sdf.v \\
        @RTL@/fft_reorder.v ]
    set_property top fft_sdf [current_fileset]
    set tw_abs  [file normalize [file join [pwd] fft_twiddles.mem]]
    synth_design -top fft_sdf \\
        -generic NUM_POINTS=$npts \\
        -generic SAMPLE_WIDTH=16 -generic SAMPLE_DECIMAL=0 \\
        -generic OUTPUT_WIDTH=16 -generic OUTPUT_DECIMAL=0 \\
        -generic TWIDDLE_WIDTH=18 -generic TWIDDLE_DECIMAL=17 \\
        -generic SCALING_PACK=$pack -generic INTERN_WIDTH=$intern \\
        -generic TWIDDLE_FILE=$tw_abs \\
        -generic PIPE_DEPTH=10
}
set_property verilog_define {FFTGEN_PRELOADS=1} [current_fileset]
set_property include_dirs [list .] [current_fileset]
create_clock -period @NS@ -name clk [get_ports clk]
report_utilization -file util.txt
report_timing_summary -delay_type max -max_paths 5 -file timing.txt
"""

TCL_R22 = """\
set part    [lindex $argv 0]
set npts    [lindex $argv 1]
create_project -in_memory -part $part
add_files -fileset sources_1 [list \\
    [file normalize [file join [pwd] fft_core.v]] \\
    @RTL@/fft_stage_r22.v \\
    @RTL@/fft_sdf.v ]
set_property top fft_r22_top [current_fileset]
synth_design -top fft_r22_top -part $part
create_clock -period @NS@ -name clk [get_ports clk]
report_utilization -file util.txt
report_timing_summary -delay_type max -max_paths 5 -file timing.txt
"""

TCL_R22_SSR = """\
set part    [lindex $argv 0]
set npts    [lindex $argv 1]
set ssr     [lindex $argv 2]
set pack    [lindex $argv 3]
set intern  [lindex $argv 4]
create_project -in_memory -part $part
add_files -fileset sources_1 [list \\
    @RTL@/fft_ssr_r22.v \\
    @RTL@/fft_top_r22.v \\
    @RTL@/fft_sdf_r22.v \\
    @RTL@/fft_stage_r22.v \\
    @RTL@/fft_sdf.v \\
    @RTL@/fft_reorder.v \\
    @RTL@/fft_cross.v ]
set_property top fft_ssr_r22 [current_fileset]
set wn_abs  [file normalize [file join [pwd] fft_wn.mem]]
set tw_abs  [file normalize [file join [pwd] fft_twiddles_r22_lane.mem]]
synth_design -top fft_ssr_r22 \\
    -generic NUM_POINTS=$npts -generic SSR=$ssr \\
    -generic WN_FILE=$wn_abs -generic LANE_TW_FILE=$tw_abs \\
    -generic SAMPLE_WIDTH=16 -generic SAMPLE_DECIMAL=0 \\
    -generic OUTPUT_WIDTH=16 -generic OUTPUT_DECIMAL=0 \\
    -generic TWIDDLE_WIDTH=18 -generic TWIDDLE_DECIMAL=17 \\
    -generic SCALING_PACK=$pack -generic INTERN_WIDTH=$intern \\
    -generic PIPE_DEPTH=10 -generic INVERSE=0
create_clock -period @NS@ -name clk [get_ports clk]
report_utilization -file util.txt
report_timing_summary -delay_type max -max_paths 5 -file timing.txt
"""

UTIL_ROWS = ["CLB LUTs", "CLB Registers", "LUT as Memory",
             "Block RAM Tile", "URAM", "DSPs"]

# ---------------------------------------------------------------------------
# artifact helpers
# ---------------------------------------------------------------------------

def write_r22_twiddle_mem(cfg, path):
    """R2^2 twiddle ROM: pair m occupies [BASE_m, BASE_m+3*D_m) with slices
    [T[g*4^m]], [T[2g*4^m]], [T[3g*4^m]] for g in [0,D_m). Leftover odd stage
    appends one W^0 word. Mirrors spikes/S5_r22/rtl_check.py."""
    from twiddles import canonical_twiddles
    N = cfg.num_points
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal, cfg.inverse)
    words = []
    m = 0
    while 2 * m + 1 < cfg.num_stages:
        D = N >> (2 * m + 2)
        base = 4 ** m
        for which in (1, 2, 3):
            for g in range(D):
                re, im = tw[(which * g * base) % N]
                words.append(((re & ((1 << cfg.twiddle_width) - 1)) << cfg.twiddle_width)
                             | (im & ((1 << cfg.twiddle_width) - 1)))
        m += 1
    if cfg.num_stages % 2 == 1:
        re, im = tw[0]
        words.append(((re & ((1 << cfg.twiddle_width) - 1)) << cfg.twiddle_width)
                     | (im & ((1 << cfg.twiddle_width) - 1)))
    hwidth = (cfg.twiddle_width * 2 + 3) // 4
    with open(path, "w") as f:
        for w in words:
            f.write("%0*x\n" % (hwidth, w))


def artifacts_r2(n, r, outdir):
    """Write r2 twiddle ROMs + preload pack; return generics."""
    from config import FFTConfig
    from fft_gen import (write_twiddle_mem, write_lane_twiddle_mem,
                         write_wn_mem, write_preload_pack_vh)
    from golden import SDFGoldenModel
    if r > 1:
        full = FFTConfig(num_points=n, ssr=r, output_order="native")
        lane = dataclasses.replace(full, num_points=n // r, ssr=1,
                                   input_order="native",
                                   output_order="bitreversed")
        write_lane_twiddle_mem(lane, os.path.join(outdir, "fft_twiddles_lane.mem"))
        write_wn_mem(full, os.path.join(outdir, "fft_wn.mem"))
        gm = SDFGoldenModel(lane, dit=False)
        pack = sum((sh & 3) << (2 * s) for s, sh in enumerate(lane.shifts))
        intern = lane.sample_width + max(0, lane.num_stages - sum(lane.shifts)) + 1
    else:
        cfg = FFTConfig(num_points=n)
        write_twiddle_mem(cfg, os.path.join(outdir, "fft_twiddles.mem"))
        gm = SDFGoldenModel(dataclasses.replace(
            cfg, input_order="native", output_order="bitreversed"), dit=False)
        pack = sum((sh & 3) << (2 * s) for s, sh in enumerate(cfg.shifts))
        intern = cfg.sample_width + max(0, cfg.num_stages - sum(cfg.shifts)) + 1
    bits = write_preload_pack_vh(gm.stage_preloads, os.path.join(outdir, "fft_preloads.vh"))
    return {"pack": pack, "intern": intern, "preload_bits": bits}


def artifacts_r22(n, outdir, r=1):
    """Write r22 twiddle ROM(s) + spike top (R=1) or SSR files (R>1); return generics."""
    from config import FFTConfig
    from fft_gen import write_wn_mem
    if r == 1:
        cfg = FFTConfig(num_points=n)
        write_r22_twiddle_mem(cfg, os.path.join(outdir, "fft_twiddles_r22.mem"))
        shutil.copy(os.path.join(outdir, "fft_twiddles_r22.mem"),
                    os.path.join(outdir, "fft_twiddles.mem"))
        try:
            sys.path.insert(0, SPIKE_R22)
            from top_gen import top_rtl
            top, _lat = top_rtl(cfg)
        except Exception as e:
            raise RuntimeError(f"r22 top_gen failed for N={n}: {e}")
        finally:
            if SPIKE_R22 in sys.path:
                sys.path.remove(SPIKE_R22)
        open(os.path.join(outdir, "fft_core.v"), "w").write(top)
        pack = sum((sh & 3) << (2 * s) for s, sh in enumerate(cfg.shifts))
        intern = cfg.sample_width + max(0, cfg.num_stages - sum(cfg.shifts)) + 1
        return {"pack": pack, "intern": intern, "preload_bits": 0}
    else:
        # SSR R>1: R x M-point r22 lanes + crossbar (reuse plain WN layout)
        full = FFTConfig(num_points=n, ssr=r, output_order="native")
        lane = dataclasses.replace(full, num_points=n // r, ssr=1,
                                   input_order="native", output_order="bitreversed")
        # M-point r22 lane twiddle ROM (3*D per pair layout)
        write_r22_twiddle_mem(lane, os.path.join(outdir, "fft_twiddles_r22_lane.mem"))
        # keep compat copies for any plain fallback
        shutil.copy(os.path.join(outdir, "fft_twiddles_r22_lane.mem"),
                    os.path.join(outdir, "fft_twiddles_lane.mem"))
        write_wn_mem(full, os.path.join(outdir, "fft_wn.mem"))
        # lane preload not needed (K_PRELOAD per pair); use lane's shifts for pack/intern
        from golden import R22SDFGoldenModel
        gm = R22SDFGoldenModel(lane)
        pack = sum((sh & 3) << (2 * s) for s, sh in enumerate(lane.shifts))
        intern = lane.sample_width + max(0, lane.num_stages - sum(lane.shifts)) + 1
        # no preload pack for r22 (per-pair K_PRELOAD); keep file for compat
        open(os.path.join(outdir, "fft_preloads.vh"), "w").write("// r22: K_PRELOAD per pair\n")
        return {"pack": pack, "intern": intern, "preload_bits": 0, "lane": lane}


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------

def parse_util(path):
    res = {}
    if not os.path.isfile(path):
        return res
    for line in open(path):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[1].rstrip("*") in UTIL_ROWS:
            raw = parts[2].strip()
            try:
                if "." in raw:
                    v = float(raw)
                    res[parts[1].rstrip("*")] = (int(v) if v.is_integer() else v)
                else:
                    res[parts[1].rstrip("*")] = int(raw)
            except ValueError:
                pass
    return res


def parse_timing(path):
    if not os.path.isfile(path):
        return {}
    for line in open(path):
        s = line.strip()
        if s.startswith("clk ") and len(s.split()) >= 7:
            f = s.split()
            try:
                return {"wns": float(f[1]), "tns": float(f[2]),
                        "failing": int(f[3]), "endpoints": int(f[4])}
            except (ValueError, IndexError):
                continue
    return {}

# ---------------------------------------------------------------------------
# per-config runner
# ---------------------------------------------------------------------------

def run_one(args):
    n, r, arch, jobs_dir = args
    tag = f"N{n}_R{r}_{arch}"
    outdir = os.path.join(jobs_dir, tag)
    res_path = os.path.join(outdir, "result.json")
    if os.path.isfile(res_path):
        cached = json.load(open(res_path))
        if cached.get("rc") == 0:
            return tag, cached, True
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    gen = {}
    try:
        if arch == "r22":
            gen = artifacts_r22(n, outdir, r=r)
        else:
            gen = artifacts_r2(n, r, outdir)
    except Exception as e:
        res = {"rc": 2, "error": f"artifacts: {e}", "arch": arch}
        json.dump(res, open(res_path, "w"), indent=1)
        return tag, res, False

    # write TCL
    if arch == "r22":
        if r == 1:
            tcl = TCL_R22.replace("@RTL@", os.path.join(ROOT, "rtl")).replace("@NS@", str(CLK_NS))
            tcl_path = os.path.join(outdir, "synth.tcl")
            open(tcl_path, "w").write(tcl)
            cmd = [VIVADO, "-mode", "batch", "-nojournal", "-nolog",
                   "-source", "synth.tcl", "-tclargs", PART, str(n)]
        else:
            tcl = TCL_R22_SSR.replace("@RTL@", os.path.join(ROOT, "rtl")).replace("@NS@", str(CLK_NS))
            tcl_path = os.path.join(outdir, "synth.tcl")
            open(tcl_path, "w").write(tcl)
            cmd = [VIVADO, "-mode", "batch", "-nojournal", "-nolog",
                   "-source", "synth.tcl", "-tclargs", PART, str(n), str(r),
                   str(gen["pack"]), str(gen["intern"])]
    else:
        tcl = TCL_R2.replace("@RTL@", os.path.join(ROOT, "rtl")).replace("@NS@", str(CLK_NS))
        tcl_path = os.path.join(outdir, "synth.tcl")
        open(tcl_path, "w").write(tcl)
        cmd = [VIVADO, "-mode", "batch", "-nojournal", "-nolog",
               "-source", "synth.tcl", "-tclargs", PART, str(n), str(r),
               str(gen["pack"]), str(gen["intern"])]
    p = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    rc = p.returncode
    res = {"rc": rc, "arch": arch, "n": n, "r": r,
           "util": parse_util(os.path.join(outdir, "util.txt")),
           "timing": parse_timing(os.path.join(outdir, "timing.txt")),
           "preload_bits": gen.get("preload_bits", 0),
           "secs": round(time.time() - t0, 1)}
    if rc != 0:
        res["error"] = (p.stderr or p.stdout or "vivado failed")[-600:]
    json.dump(res, open(res_path, "w"), indent=1)
    return tag, res, False

# ---------------------------------------------------------------------------
# table formatting
# ---------------------------------------------------------------------------

def fmt_table(rows, header):
    md = ["| " + " | ".join(header) + " |",
          "|" + "|".join("---:" if h not in ("Arch",) else ":---:" for h in header) + "|"]
    for row in rows:
        md.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--r1", nargs="*", type=int,
                    default=[64, 128, 256, 512, 1024, 2048, 4096, 8192])
    ap.add_argument("--r2", nargs="*", type=int,
                    default=[64, 128, 256, 512, 1024, 2048, 4096, 8192])
    ap.add_argument("--r4", nargs="*", type=int, default=[64, 256, 1024, 4096])
    ap.add_argument("--r8", nargs="*", type=int, default=[64, 256, 1024])
    ap.add_argument("--arch", choices=["r2", "r22", "both"], default="both",
                    help="which architecture(s) to sweep (default: both)")
    ap.add_argument("-j", type=int, default=4, help="parallel vivado jobs")
    ap.add_argument("--jobs-dir", default=os.path.join(ROOT, "build", "datasheet"),
                    help="output directory for per-config results")
    ap.add_argument("--keep-spike-dir", action="store_true",
                    help="also mirror tables into spikes/S2_timing/build/datasheet")
    args = ap.parse_args()

    archs = {"r2": ["r2"], "r22": ["r22"], "both": ["r2", "r22"]}[args.arch]

    # Build config list: r22 for R=1 and R=2 (P7), r2 for all R
    configs = []
    for n in args.r1:
        if "r2" in archs:
            configs.append((n, 1, "r2"))
        if "r22" in archs:
            configs.append((n, 1, "r22"))
    for n in args.r2:
        if "r2" in archs:
            configs.append((n, 2, "r2"))
        if "r22" in archs:
            configs.append((n, 2, "r22"))
    for n in args.r4:
        if "r2" in archs:
            configs.append((n, 4, "r2"))
        # R=4 r22 is next (needs lane M-point r22 + 4-pt crossbar); not yet wired
    for n in args.r8:
        if "r2" in archs:
            configs.append((n, 8, "r2"))

    os.makedirs(args.jobs_dir, exist_ok=True)
    print(f"sweep: {len(configs)} configs ({', '.join(archs)}), {args.j} parallel -> {args.jobs_dir}")

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.j) as ex:
        futs = {ex.submit(run_one, (n, r, a, args.jobs_dir)): (n, r, a) for n, r, a in configs}
        for fut in concurrent.futures.as_completed(futs):
            tag, res, cached = fut.result()
            done += 1
            status = "cached" if cached else ("ok" if res.get("rc") == 0 else f"FAILED: {res.get('error','')[:50]}")
            u, t = res.get("util", {}), res.get("timing", {})
            wns = f"{t['wns']:+.3f}" if "wns" in t else "?"
            secs = res.get('secs', '?')
            secs_s = f"{secs:.1f}" if isinstance(secs, (int, float)) else str(secs)
            print(f"[{done:2d}/{len(configs)}] {tag:14s} {secs_s:>6s}s  {status:10s}  WNS={wns}  DSP={u.get('DSPs','?')} LUT={u.get('CLB LUTs','?')} BRAM={u.get('Block RAM Tile','?')}")

    # ---- per-arch table (Arch column) + side-by-side R=1 comparison ----
    def load(n, r, arch):
        rp = os.path.join(args.jobs_dir, f"N{n}_R{r}_{arch}", "result.json")
        return json.load(open(rp)) if os.path.isfile(rp) else {}

    header = ["N", "R", "Arch", "LUTs", "FFs", "LUTRAM", "DSP", "BRAM36", "URAM", "WNS(ns)", "FEP", "clk/frame"]
    table = []
    for r in (1, 2, 4, 8):
        ns = sorted(set(n for n_, r_, a in configs if r_ == r for n in [n_]))
        for n in ns:
            for arch in archs:
                if arch == "r22" and r not in (1, 2):
                    continue
                res = load(n, r, arch)
                if res.get("rc") != 0:
                    continue
                u, t = res.get("util", {}), res.get("timing", {})
                table.append([n, r, arch,
                              u.get("CLB LUTs", "-"), u.get("CLB Registers", "-"),
                              u.get("LUT as Memory", "-"), u.get("DSPs", "-"),
                              u.get("Block RAM Tile", "-"), u.get("URAM", "-"),
                              f"{t['wns']:+.3f}" if "wns" in t else "-",
                              str(t.get("failing", "-")), n // max(1, r)])

    md = "# fftgen datasheet -- KU5P OOC synthesis @ 500 MHz\n\n"
    md += "xcku5p-ffva676-1-e, Vivado 2026.1 out-of-context, create_clock 2.0 ns, "
    md += "16-bit samples / 18-bit twiddles, auto scaling, PIPE_DEPTH=10. "
    md += "Post-synth estimates; worst-case paths close post-route (see PLAN.md P5a).\n\n"
    if "r22" in archs:
        md += "Arch: ``r2`` = plain radix-2 SDF (log2(N) stages, P6 trivial reduction, "
        md += "``4x(stages-2)`` DSPs/engine); ``r22`` = radix-2^2 SDF (P7, one multiply/pair, "
        md += "``spikes/S5_r22`` top + ``rtl/fft_ssr_r22`` for R>1). r22 re-pins the golden rounding (few-LSB, SQNR-equal).\n\n"
    md += fmt_table(table, header) + "\n"

    # side-by-side R=1 and R=2 comparisons (when both arches present)
    if "r2" in archs and "r22" in archs:
        comp_header = ["N", "R", "R2 DSP", "R22 DSP", "ΔDSP", "R2 LUTs", "R22 LUTs", "ΔLUT", "R2 WNS", "R22 WNS", "R2 BRAM", "R22 BRAM"]
        comp_rows_r1, comp_rows_r2 = [], []
        for n in sorted(set(x for x, _, _ in [(n, r, a) for n, r, a in configs if r == 1])):
            r2 = load(n, 1, "r2"); r22 = load(n, 1, "r22")
            if r2.get("rc") != 0 or r22.get("rc") != 0: continue
            u2, t2 = r2.get("util", {}), r2.get("timing", {})
            u22, t22 = r22.get("util", {}), r22.get("timing", {})
            dsp2, dsp22 = u2.get("DSPs", "-"), u22.get("DSPs", "-")
            lut2, lut22 = u2.get("CLB LUTs", "-"), u22.get("CLB LUTs", "-")
            d_dsp = f"{dsp22 - dsp2:+d}" if isinstance(dsp2, int) and isinstance(dsp22, int) else "-"
            d_lut = f"{lut22 - lut2:+d}" if isinstance(lut2, int) and isinstance(lut22, int) else "-"
            comp_rows_r1.append([n, 1, dsp2, dsp22, d_dsp, lut2, lut22, d_lut,
                              f"{t2.get('wns',0):+.3f}" if "wns" in t2 else "-",
                              f"{t22.get('wns',0):+.3f}" if "wns" in t22 else "-",
                              u2.get("Block RAM Tile", "-"), u22.get("Block RAM Tile", "-")])
        for n in sorted(set(x for x, _, _ in [(n, r, a) for n, r, a in configs if r == 2])):
            r2 = load(n, 2, "r2"); r22 = load(n, 2, "r22")
            if r2.get("rc") != 0 or r22.get("rc") != 0: continue
            u2, t2 = r2.get("util", {}), r2.get("timing", {})
            u22, t22 = r22.get("util", {}), r22.get("timing", {})
            dsp2, dsp22 = u2.get("DSPs", "-"), u22.get("DSPs", "-")
            lut2, lut22 = u2.get("CLB LUTs", "-"), u22.get("CLB LUTs", "-")
            d_dsp = f"{dsp22 - dsp2:+d}" if isinstance(dsp2, int) and isinstance(dsp22, int) else "-"
            d_lut = f"{lut22 - lut2:+d}" if isinstance(lut2, int) and isinstance(lut22, int) else "-"
            comp_rows_r2.append([n, 2, dsp2, dsp22, d_dsp, lut2, lut22, d_lut,
                              f"{t2.get('wns',0):+.3f}" if "wns" in t2 else "-",
                              f"{t22.get('wns',0):+.3f}" if "wns" in t22 else "-",
                              u2.get("Block RAM Tile", "-"), u22.get("Block RAM Tile", "-")])
        if comp_rows_r1:
            md += "\n## R=1 comparison: r2 vs r22 (DSP savings)\n\n"
            md += fmt_table(comp_rows_r1, comp_header) + "\n"
        if comp_rows_r2:
            md += "\n## R=2 comparison: r2 vs r22 (SSR)\n\n"
            md += fmt_table(comp_rows_r2, comp_header) + "\n"
        md += "\n*ΔDSP negative = r22 saves DSPs (target ~-50%); ΔLUT shows fabric cost. R=2 r22 uses M-point r22 lanes (M=N/2) + same crossbar.*\n"
        comp_rows = comp_rows_r1 + comp_rows_r2
    else:
        comp_rows = []

    out_md = os.path.join(args.jobs_dir, "datasheet.md")
    out_csv = os.path.join(args.jobs_dir, "datasheet.csv")
    with open(out_md, "w") as f:
        f.write(md + "\n")
    with open(out_csv, "w") as f:
        f.write(",".join(header) + "\n")
        for row in table:
            f.write(",".join(str(x) for x in row) + "\n")
    print("\n" + md)
    print(f"tables: {out_md}")
    # also write comparison csv for R=1 when both
    if "r2" in archs and "r22" in archs and comp_rows:
        comp_csv = os.path.join(args.jobs_dir, "comparison_r1.csv")
        with open(comp_csv, "w") as f:
            f.write(",".join(comp_header) + "\n")
            for r in comp_rows:
                f.write(",".join(str(x) for x in r) + "\n")
        print(f"comparison: {comp_csv}")

    # optional mirror into spike dir for backwards compat
    if args.keep_spike_dir:
        spike_dir = os.path.join(ROOT, "spikes", "S2_timing", "build", "datasheet")
        os.makedirs(spike_dir, exist_ok=True)
        shutil.copy(out_md, os.path.join(spike_dir, "datasheet.md"))
        shutil.copy(out_csv, os.path.join(spike_dir, "datasheet.csv"))

if __name__ == "__main__":
    main()
