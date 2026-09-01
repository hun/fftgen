"""S7 r2^3 probe: does the 45-degree (W8) rotation fit the proven r22
pipeline at 2 ns, and where?

Synthesizes ONE stage (D=128, 16b data, 18b twiddle, Q8 = 92682) OOC on
KU5P at 2.0 ns per variant and prints WNS + worst-path endpoints + DSP48
count.  Variants (see mk_variants.py):

  base          the S5 `nat` stage (+Q8 param) -- 45° rides the shared
                cmul as ROM words (the PLAN's 4*floor(n/3) accounting)
  w8_pre        operand-side fabric rotate, combinational into the AREG
  w8_pre_pipe   w8_pre + one own register before the AREG
  w8_post       product-side fabric rotate after the DSP PREG, into the
                existing shift stage
  w8_post_pipe  product-side tap tree + own register, shift in next hop

Timing probe only -- values are NOT the r2^3 contract (rot45.py pins the
numeric candidates; the golden model chooses after this probe).

Run:  python3 spikes/S7_r23/probe.py [variant ...]
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
S5 = os.path.abspath(os.path.join(HERE, "..", "S5_r22"))
VIVADO = "/tools/Xilinx/2026.1/Vivado/bin/vivado"

sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, S5)

TCL = """
create_project -in_memory -part xcku5p-ffva676-1-e
read_verilog probe_top.v
read_verilog fft_stage_r22.v
synth_design -top fft_probe_top -part xcku5p-ffva676-1-e
create_clock -period 2.000 -name clk [get_ports clk]
report_utilization -file utilization.rpt
report_timing_summary -delay_type max -max_paths 5 -nworst 2 -file timing.rpt
set dsps [get_cells -hier -filter {REF_NAME == DSP48E2}]
puts "DSP48: [llength $dsps]"
exit
"""


def run(tag):
    outdir = os.path.join(HERE, "build_" + tag)
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    shutil.copy(os.path.join(HERE, "variants", f"fft_stage_{tag}.v"),
                os.path.join(outdir, "fft_stage_r22.v"))
    shutil.copy(os.path.join(HERE, "probe_top.v"), outdir)
    from config import FFTConfig
    from rtl_check import write_r22_twiddle_mem
    write_r22_twiddle_mem(FFTConfig(num_points=512),
                          os.path.join(outdir, "fft_twiddles_r22.mem"))
    open(os.path.join(outdir, "synth.tcl"), "w").write(TCL)
    with open(os.path.join(outdir, "vivado.log"), "w") as fh:
        r = subprocess.run([VIVADO, "-mode", "batch", "-source", "synth.tcl"],
                           cwd=outdir, stdout=fh, stderr=subprocess.STDOUT)
    log = open(os.path.join(outdir, "vivado.log")).read()
    dsp = re.search(r"DSP48: (\d+)", log)
    trpt = open(os.path.join(outdir, "timing.rpt")).read()
    wns = re.search(r"^\s+(-?[\d.]+)\s+-?[\d.]+\s+(\d+)\s+\d+\s+", trpt, re.M)
    met = "All user specified timing constraints are met." in trpt
    src = re.findall(r"^  Source:\s+(\S+)", trpt, re.M)
    dst = re.findall(r"^  Destination:\s+(\S+)", trpt, re.M)
    lv = re.findall(r"Logic Levels:\s+(\d+)\s+\(([^)]*)\)", trpt)
    lut = re.search(r"CLB LUTs\*\s+\|\s*(\d+)", open(
        os.path.join(outdir, "utilization.rpt")).read())
    print(f"[{tag}] rc={r.returncode} DSP={dsp.group(1) if dsp else '?'} "
          f"LUT={lut.group(1) if lut else '?'} "
          f"WNS={'MET' if met else (wns.group(1) if wns else '?')} "
          f"failEP={0 if met else (wns.group(2) if wns else '?')}")
    for i, (s, d) in enumerate(zip(src, dst)):
        l = lv[i] if i < len(lv) else ("?", "?")
        print(f"    {l[0]} lvl ({l[1].strip()}): "
              f"{s.split('/')[-2] if '/' in s else s} -> {d.split('/')[-1]}")
    return outdir


if __name__ == "__main__":
    variants = sys.argv[1:] or ["base", "w8_pre", "w8_pre_pipe",
                                "w8_post", "w8_post_pipe"]
    for v in variants:
        run(v)
