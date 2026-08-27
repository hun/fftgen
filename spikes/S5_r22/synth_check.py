"""Spike S5f: R2^2 core synthesis -- DSP count + timing proof (P7).

Generates the full R2^2 core (spike top generator) for a config, writes
the synth tree (RTL + twiddle ROM + a Vivado OOC tcl), runs synthesis on
KU5P @ 500 MHz, and reports the DSP count + WNS.
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "src")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from top_gen import top_rtl
from rtl_check import write_r22_twiddle_mem
from config import FFTConfig

SPIKE = os.path.dirname(os.path.abspath(__file__))
RTL = os.path.join(SPIKE, "..", "..", "rtl")


def synth(cfg, part="xcku5p-ffva676-1-e", clk_mhz=500.0, impl=False):
    N = cfg.num_points
    outdir = os.path.join(SPIKE, f"synth_n{N}")
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    for fn in ("fft_stage_r22.v",):
        shutil.copy(os.path.join(RTL, fn), outdir)
    shutil.copy(os.path.join(RTL, "fft_sdf.v"), outdir)
    write_r22_twiddle_mem(cfg, os.path.join(outdir, "fft_twiddles_r22.mem"))
    top, lat = top_rtl(cfg)
    open(os.path.join(outdir, "fft_core.v"), "w").write(top)

    period = 1000.0 / clk_mhz
    tcl = f"""
set part   "{part}"
set period_ns {period:.3f}
create_project -in_memory -part $part
read_verilog fft_core.v
read_verilog fft_stage_r22.v
read_verilog fft_sdf.v
synth_design -top fft_r22_top -part $part
create_clock -period $period_ns -name clk [get_ports clk]
report_utilization -file utilization.rpt
report_timing_summary -delay_type max -max_paths 10 -file timing.rpt
{"opt_design\nplace_design\nphys_opt_design\nroute_design\nphys_opt_design\nroute_design\nreport_utilization -file utilization_impl.rpt\nreport_timing_summary -delay_type max -max_paths 10 -file timing_impl.rpt" if impl else ""}
"""
    open(os.path.join(outdir, "synth.tcl"), "w").write(tcl)
    r = subprocess.run(
        ["/tools/Xilinx/2026.1/Vivado/bin/vivado", "-mode", "batch",
         "-source", "synth.tcl"],
        cwd=outdir, capture_output=True, text=True, timeout=3600)
    return outdir, r.returncode


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--num-points", type=int, default=2048)
    p.add_argument("--impl", action="store_true")
    args = p.parse_args()
    cfg = FFTConfig(num_points=args.num_points)
    outdir, rc = synth(cfg, impl=args.impl)
    print(f"synth rc={rc}")
    for line in open(os.path.join(outdir, "utilization.rpt")):
        if "DSP" in line or "LUTs" in line or "URAM" in line or "BRAM" in line:
            print("  ", line.strip())
    import re
    for line in open(os.path.join(outdir, "timing.rpt")):
        m = re.match(r"\s+([\d.-]+)\s+([\d.-]+)\s+0\s+\d+", line)
        if m:
            print("  WNS:", m.group(1), "TNS:", m.group(2))
