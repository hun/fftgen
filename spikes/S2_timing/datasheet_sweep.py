#!/usr/bin/env python3
"""Datasheet sweep: OOC synthesis across N and R on KU5P @ 500 MHz.

For each (N, R) config this writes the generator artifacts (twiddle
ROMs + post-warm preload pack), runs Vivado out-of-context synthesis,
and parses resource utilization + timing into a combined table.

Results are cached per config in <outdir>/results.json; re-runs skip
completed configs, so an interrupted sweep resumes where it stopped.

    python3 spikes/S2_timing/datasheet_sweep.py -j 4
    python3 spikes/S2_timing/datasheet_sweep.py --r1 64 1024 --r2 256
"""

import argparse
import concurrent.futures
import dataclasses
import json
import os
import re
import subprocess
import sys
import time

# script lives in <repo>/spikes/S2_timing/
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

VIVADO = os.environ.get("VIVADO_BIN",
                        "/tools/Xilinx/2026.1/Vivado/bin/vivado")
PART = "xcku5p-ffva676-1-e"
CLK_NS = 2.0

TCL = """\
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

UTIL_ROWS = ["CLB LUTs", "CLB Registers", "LUT as Memory",
             "Block RAM Tile", "URAM", "DSPs"]


def artifacts(n: int, r: int, outdir: str) -> dict:
    """Write twiddle ROMs + preload pack; return generics."""
    from config import FFTConfig
    from fft_gen import (write_twiddle_mem, write_lane_twiddle_mem,
                         write_wn_mem, write_preload_pack_vh)
    from golden import SDFGoldenModel

    if r > 1:
        full = FFTConfig(num_points=n, ssr=r, output_order="native")
        lane = dataclasses.replace(full, num_points=n // r, ssr=1,
                                   input_order="native",
                                   output_order="bitreversed")
        write_lane_twiddle_mem(lane, os.path.join(outdir,
                                                  "fft_twiddles_lane.mem"))
        write_wn_mem(full, os.path.join(outdir, "fft_wn.mem"))
        gm = SDFGoldenModel(lane, dit=False)
        pack = sum((sh & 3) << (2 * s) for s, sh in enumerate(lane.shifts))
        intern = (lane.sample_width
                  + max(0, lane.num_stages - sum(lane.shifts)) + 1)
    else:
        cfg = FFTConfig(num_points=n)
        write_twiddle_mem(cfg, os.path.join(outdir, "fft_twiddles.mem"))
        gm = SDFGoldenModel(dataclasses.replace(
            cfg, input_order="native", output_order="bitreversed"),
            dit=False)
        pack = sum((sh & 3) << (2 * s) for s, sh in enumerate(cfg.shifts))
        intern = (cfg.sample_width
                  + max(0, cfg.num_stages - sum(cfg.shifts)) + 1)
    bits = write_preload_pack_vh(gm.stage_preloads,
                                 os.path.join(outdir, "fft_preloads.vh"))
    return {"pack": pack, "intern": intern, "preload_bits": bits}


def parse_util(path: str) -> dict:
    res = {}
    if not os.path.isfile(path):
        return res
    for line in open(path):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[1].rstrip("*") in UTIL_ROWS:
            raw = parts[2].strip()
            try:
                # Block RAM Tile reports halves (e.g. 10.5); keep as float
                # and render without trailing .0 for integers.
                if "." in raw:
                    v = float(raw)
                    res[parts[1].rstrip("*")] = (int(v) if v.is_integer() else v)
                else:
                    res[parts[1].rstrip("*")] = int(raw)
            except ValueError:
                pass
    return res


def parse_timing(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    for line in open(path):
        s = line.strip()
        if s.startswith("clk ") and len(s.split()) >= 7:
            f = s.split()
            try:
                wns = float(f[1])
                tns = float(f[2])
                fail = int(f[3])
                total = int(f[4])
                return {"wns": wns, "tns": tns, "failing": fail,
                        "endpoints": total}
            except (ValueError, IndexError):
                continue
    return {}


def run_one(args):
    n, r, jobs_dir = args
    name = "N%d_R%d" % (n, r)
    outdir = os.path.join(jobs_dir, name)
    res_path = os.path.join(outdir, "result.json")

    if os.path.isfile(res_path):
        cached = json.load(open(res_path))
        if cached.get("rc") == 0:
            return name, cached, True   # cached

    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    gen = {}
    try:
        gen = artifacts(n, r, outdir)
    except Exception as e:
        res = {"rc": 2, "error": "artifacts: %s" % e}
        json.dump(res, open(res_path, "w"), indent=1)
        return name, res, False

    tcl_path = os.path.join(outdir, "synth.tcl")
    with open(tcl_path, "w") as f:
        f.write(TCL.replace("@RTL@", os.path.join(ROOT, "rtl"))
             .replace("@NS@", str(CLK_NS)))
    cmd = [VIVADO, "-mode", "batch", "-nojournal", "-nolog",
           "-source", "synth.tcl", "-tclargs", PART, str(n), str(r),
           str(gen["pack"]), str(gen["intern"])]
    p = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    rc = p.returncode
    res = {
        "rc": rc,
        "util": parse_util(os.path.join(outdir, "util.txt")),
        "timing": parse_timing(os.path.join(outdir, "timing.txt")),
        "preload_bits": gen["preload_bits"],
        "secs": round(time.time() - t0, 1),
    }
    if rc != 0:
        res["error"] = p.stderr[-400:] if p.stderr else "vivado failed"
    json.dump(res, open(res_path, "w"), indent=1)
    return name, res, False


def fmt_table(rows, header):
    md = ["| " + " | ".join(header) + " |",
          "|" + "|".join("---:" for _ in header) + "|"]
    for row in rows:
        md.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--r1", nargs="*", type=int,
                    default=[64, 128, 256, 512, 1024, 2048, 4096, 8192])
    ap.add_argument("--r2", nargs="*", type=int,
                    default=[64, 128, 256, 512, 1024, 2048, 4096, 8192])
    ap.add_argument("--r4", nargs="*", type=int,
                    default=[64, 256, 1024, 4096])
    ap.add_argument("--r8", nargs="*", type=int, default=[64, 256, 1024])
    ap.add_argument("-j", type=int, default=4, help="parallel vivado jobs")
    ap.add_argument("--jobs-dir",
                    default=os.path.join(os.path.dirname(__file__),
                                         "build", "datasheet"))
    args = ap.parse_args()

    configs = ([(n, 1) for n in args.r1] + [(n, 2) for n in args.r2]
               + [(n, 4) for n in args.r4] + [(n, 8) for n in args.r8])
    os.makedirs(args.jobs_dir, exist_ok=True)
    print("sweep: %d configs, %d parallel jobs -> %s"
          % (len(configs), args.j, args.jobs_dir))

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.j) as ex:
        futures = {ex.submit(run_one, (n, r, args.jobs_dir)): (n, r)
                   for n, r in configs}
        for fut in concurrent.futures.as_completed(futures):
            name, res, cached = fut.result()
            done += 1
            status = ("cached" if cached
                      else ("ok" if res.get("rc") == 0
                            else "FAILED: %s" % res.get("error", "")))
            u, t = res.get("util", {}), res.get("timing", {})
            print("[%2d/%2d] %-12s %6ss  %s  WNS=%s  DSP=%s LUT=%s BRAM=%s"
                  % (done, len(configs), name, res.get("secs", "?"), status,
                     ("%+.3f" % t["wns"]) if "wns" in t else "?",
                     u.get("DSPs", "?"), u.get("CLB LUTs", "?"),
                     u.get("Block RAM Tile", "?")))

    # ---- tables ----
    def row(n, r):
        rp = os.path.join(args.jobs_dir, "N%d_R%d" % (n, r), "result.json")
        res = json.load(open(rp)) if os.path.isfile(rp) else {}
        if res.get("rc") != 0:
            return None
        u, t = res.get("util", {}), res.get("timing", {})
        lat_den = n // max(1, r)
        return [n, r,
                u.get("CLB LUTs", "-"), u.get("CLB Registers", "-"),
                u.get("LUT as Memory", "-"), u.get("DSPs", "-"),
                u.get("Block RAM Tile", "-"), u.get("URAM", "-"),
                ("%+.3f" % t["wns"]) if "wns" in t else "-",
                ("%d" % t["failing"]) if "failing" in t else "-",
                lat_den]

    table = []
    for r in (1, 2, 4, 8):
        ns = sorted(n for m, rr in configs if rr == r for n in [m])
        for n in ns:
            row_ = row(n, r)
            if row_:
                table.append(row_)
    header = ["N", "R", "LUTs", "FFs", "LUTRAM", "DSP", "BRAM36", "URAM",
              "WNS(ns)", "FEP", "clk/frame"]
    md = "# fftgen datasheet -- KU5P OOC synthesis @ 500 MHz\n\n"
    md += ("xcku5p-ffva676-1-e, Vivado 2026.1 out-of-context, "
           "create_clock %.1f ns, 16-bit samples / 18-bit twiddles,\n"
           "auto scaling, PIPE_DEPTH=10 (NLAYERS). Post-synth estimates;\n"
           "worst-case paths close post-route (see PLAN.md P5a).\n\n"
           % CLK_NS)
    md += fmt_table(table, header) + "\n"
    with open(os.path.join(args.jobs_dir, "datasheet.md"), "w") as f:
        f.write(md + "\n")
    with open(os.path.join(args.jobs_dir, "datasheet.csv"), "w") as f:
        f.write(",".join(header) + "\n")
        for row_ in table:
            f.write(",".join(str(x) for x in row_) + "\n")
    print("\n" + md)
    print("tables: %s" % os.path.join(args.jobs_dir, "datasheet.md"))


if __name__ == "__main__":
    main()
