"""P7 crossbar timing probe: fft_cross variants, one OOC synth each.

The SSR (R>=2) 500 MHz limiter is inside the crossbar, not the lanes
(P7 step 7b): u_cross/g_pre[r].pp*_reg shows the same intra-DSP
A/B-reg -> PREADD -> MULT -> ALU -> PREG hop (1.85 ns, MREG bypassed)
that the r22 stage had, because four products are formed in one clock
and the b_* combine is written with hand-rolled ext() sign extension.

    python3 spikes/S5_r22/dsp_probe/xbar_probe.py [variant ...] [-r 2 4 8]

Variants live in variants_xbar/fft_cross_<name>.v ("cur" = repo HEAD).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
RTL = os.path.join(ROOT, "rtl")
VIVADO = os.environ.get("VIVADO_BIN", "/tools/Xilinx/2026.1/Vivado/bin/vivado")
sys.path.insert(0, os.path.join(ROOT, "src"))

TCL = """
create_project -in_memory -part xcku5p-ffva676-1-e
read_verilog fft_xbar_top.v
read_verilog fft_cross.v
synth_design -top fft_xbar_top -part xcku5p-ffva676-1-e \\
    -generic NPTS=%(npts)d -generic R=%(r)d
create_clock -period 2.000 -name clk [get_ports clk]
report_utilization -file utilization.rpt
report_timing_summary -delay_type max -max_paths 4 -nworst 1 -file timing.rpt
puts "NDSP [llength [get_cells -hier -filter {REF_NAME == DSP48E2}]]"
exit
"""


def run(src, tag, r, n):
    outdir = os.path.join(HERE, f"xb_{tag}_R{r}")
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    shutil.copy(src, os.path.join(outdir, "fft_cross.v"))
    shutil.copy(os.path.join(HERE, "fft_xbar_top.v"), outdir)
    from config import FFTConfig
    from fft_gen import write_wn_mem
    write_wn_mem(FFTConfig(num_points=n, ssr=r),
                 os.path.join(outdir, "fft_wn.mem"))
    open(os.path.join(outdir, "synth.tcl"), "w").write(TCL % {
        "npts": n, "r": r})
    with open(os.path.join(outdir, "vivado.log"), "w") as fh:
        subprocess.run([VIVADO, "-mode", "batch", "-nojournal", "-nolog",
                        "-source", "synth.tcl"], cwd=outdir,
                       stdout=fh, stderr=subprocess.STDOUT)
    txt = open(os.path.join(outdir, "timing.rpt")).read()
    log = open(os.path.join(outdir, "vivado.log")).read()
    m = re.search(r"^\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(\d+)\s+(\d+)\s+", txt, re.M)
    dsp = re.search(r"NDSP (\d+)", log)
    print(f"[{tag} R={r}] DSP={dsp.group(1) if dsp else '?'} "
          f"WNS={m.group(1) if m else '?'} failEP={m.group(3) if m else '?'}")
    for s in re.findall(r"^  Source:\s+(\S+)", txt, re.M)[:2]:
        print("    from", s.replace("u_cross/", "").replace("/CLK", ""))
    for d in re.findall(r"^  Destination:\s+(\S+)", txt, re.M)[:2]:
        print("    to  ", d.replace("u_cross/", ""))
    for lv in re.findall(r"Logic Levels:\s+(\d+)\s+\(([^)]*)\)", txt)[:2]:
        print("    lvl ", lv[0], lv[1].strip())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("variants", nargs="*", default=["cur"])
    ap.add_argument("-r", type=int, nargs="*", default=[2])
    args = ap.parse_args()
    for v in args.variants:
        src = (os.path.join(RTL, "fft_cross.v") if v == "cur"
               else (v if v.endswith(".v")
                     else os.path.join(HERE, "variants_xbar", f"fft_cross_{v}.v")))
        for r in args.r:
            run(src, os.path.splitext(os.path.basename(src))[0].replace(
                "fft_cross_", ""), r, r * 32)
