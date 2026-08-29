#!/usr/bin/env python3
"""Twiddle-ROM style sweep: distributed vs block across N (R=1).

For each N x style this synthesizes fft_sdf OOC on KU5P @ 500 MHz and
reports LUTs / LUTRAM / BRAM / WNS so the auto-cutoff for TWIDDLE_MEM
can be picked from measured data (doc/mem_cutoffs.md S4).
"""

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "datasheet"))
# reuse the datasheet sweep machinery
sys.path.insert(0, HERE)
import datasheet_sweep as ds  # noqa: E402

TCL = """\
set part    [lindex $argv 0]
set npts    [lindex $argv 1]
set style   [lindex $argv 2]
create_project -in_memory -part $part
add_files -fileset sources_1 [list \\
    @RTL@/fft_sdf.v \\
    @RTL@/fft_reorder.v ]
set_property top fft_sdf [current_fileset]
set_property verilog_define {FFTGEN_PRELOADS=1} [current_fileset]
set_property include_dirs [list .] [current_fileset]
set tw_abs [file normalize [file join [pwd] fft_twiddles.mem]]
synth_design -top fft_sdf \\
    -generic NUM_POINTS=$npts \\
    -generic SAMPLE_WIDTH=16 -generic SAMPLE_DECIMAL=0 \\
    -generic OUTPUT_WIDTH=16 -generic OUTPUT_DECIMAL=0 \\
    -generic TWIDDLE_WIDTH=18 -generic TWIDDLE_DECIMAL=17 \\
    -generic SCALING_PACK=@PACK@ -generic INTERN_WIDTH=@INTERN@ \\
    -generic TWIDDLE_FILE=$tw_abs \\
    -generic PIPE_DEPTH=10 \\
    -generic TWIDDLE_MEM=@STY@
create_clock -period @NS@ -name clk [get_ports clk]
report_utilization -file util.txt
report_timing_summary -delay_type max -max_paths 5 -file timing.txt
"""


def run_one(args, jobs_dir):
    n, style = args
    name = "N%d_%s" % (n, style)
    outdir = os.path.join(jobs_dir, name)
    res_path = os.path.join(outdir, "result.json")
    if os.path.isfile(res_path):
        cached = json.load(open(res_path))
        if cached.get("rc") == 0:
            return name, cached, True
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    gen = ds.artifacts_r2(n, 1, outdir)
    tcl = (TCL.replace("@RTL@", os.path.join(ds.ROOT, "rtl"))
              .replace("@NS@", str(ds.CLK_NS))
              .replace("@PACK@", str(gen["pack"]))
              .replace("@INTERN@", str(gen["intern"]))
              .replace("@STY@", "2" if style == "block" else "1"))
    with open(os.path.join(outdir, "synth.tcl"), "w") as f:
        f.write(tcl)
    p = subprocess.run(
        [ds.VIVADO, "-mode", "batch", "-nojournal", "-nolog",
         "-source", "synth.tcl", "-tclargs", ds.PART, str(n), style],
        cwd=outdir, capture_output=True, text=True)
    res = {"rc": p.returncode,
           "util": ds.parse_util(os.path.join(outdir, "util.txt")),
           "timing": ds.parse_timing(os.path.join(outdir, "timing.txt")),
           "secs": round(time.time() - t0, 1)}
    if p.returncode != 0:
        res["error"] = p.stderr[-300:]
    json.dump(res, open(res_path, "w"), indent=1)
    return name, res, False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ns", nargs="*", type=int,
                    default=[64, 256, 1024, 2048, 4096, 8192])
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--jobs-dir",
                    default=os.path.join(HERE, "build", "twrom"))
    args = ap.parse_args()

    configs = [(n, st) for n in args.ns
               for st in ("distributed", "block")]
    os.makedirs(args.jobs_dir, exist_ok=True)
    print("twrom sweep: %d configs" % len(configs))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.j) as ex:
        futs = {ex.submit(run_one, c, args.jobs_dir): c for c in configs}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            name, res, cached = fut.result()
            done += 1
            u, t = res.get("util", {}), res.get("timing", {})
            status = "cached" if cached else (
                "ok" if res.get("rc") == 0 else "FAILED")
            print("[%2d/%2d] %-18s %6ss %s  WNS=%s LUT=%s LUTRAM=%s BRAM=%s"
                  % (done, len(configs), name, res.get("secs", "?"), status,
                     ("%+.3f" % t["wns"]) if "wns" in t else "?",
                     u.get("CLB LUTs", "?"), u.get("LUT as Memory", "?"),
                     u.get("Block RAM Tile", "?")))

    rows = []
    for n in args.ns:
        for st in ("distributed", "block"):
            rp = os.path.join(args.jobs_dir,
                              "N%d_%s" % (n, st), "result.json")
            if not os.path.isfile(rp):
                continue
            d = json.load(open(rp))
            if d.get("rc") != 0:
                continue
            u, t = d.get("util", {}), d.get("timing", {})
            rows.append([n, st,
                         u.get("CLB LUTs", "-"),
                         u.get("CLB Registers", "-"),
                         u.get("LUT as Memory", "-"),
                         u.get("DSPs", "-"),
                         u.get("Block RAM Tile", "-"),
                         ("%.3f" % t["wns"]) if "wns" in t else "-"])
    hdr = ["N", "style", "LUTs", "FFs", "LUTRAM", "DSP", "BRAM36", "WNS"]
    md = ["| " + " | ".join(hdr) + " |",
          "|" + "|".join("---:" for _ in hdr) + "|"]
    for r in rows:
        md.append("| " + " | ".join(str(x) for x in r) + " |")
    out = "\n".join(md)
    print("\n" + out)
    with open(os.path.join(args.jobs_dir, "twrom_table.md"), "w") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
