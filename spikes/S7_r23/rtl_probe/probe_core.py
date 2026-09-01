"""S7 full-core probe: synthesize the complete fft_sdf_r23 (N=8192) OOC
on KU5P at 2 ns; report WNS, DSP48, CARRY8, LUT, FF, BRAM, worst paths."""
import os
import re
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BRINGUP_BUILD = os.path.abspath(
    os.path.join(HERE, "..", "rtl_bringup", "build"))
VIVADO = "/tools/Xilinx/2026.1/Vivado/bin/vivado"

TCL = """
create_project -in_memory -part xcku5p-ffva676-1-e
read_verilog {root}/rtl/fft_stage_r23.v
read_verilog {root}/rtl/fft_stage_r22.v
read_verilog {root}/rtl/fft_sdf_r23.v
read_verilog probe_core_top.v
synth_design -top fft_probe_core_top -part xcku5p-ffva676-1-e
create_clock -period 2.000 -name clk [get_ports clk]
place_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -delay_type max -max_paths 12 -nworst 2 -file timing.rpt
set dsps [get_cells -hier -filter {{REF_NAME == DSP48E2}}]
puts "DSP48: [llength $dsps]"
puts "CARRY8: [llength [get_cells -hier -filter {{REF_NAME == CARRY8}}]]"
puts "RAMB18: [llength [get_cells -hier -filter {{REF_NAME =~ RAMB18*}}]]"
puts "RAMB36: [llength [get_cells -hier -filter {{REF_NAME =~ RAMB36*}}]]"
exit
"""

MEMS = ["fft_tw_r23_t0.mem", "fft_tw_r23_t1.mem", "fft_tw_r23_t2.mem",
        "fft_tw_r22_l0.mem", "fft_tw_r22_l1.mem"]


def run(tag="core"):  # full implementation
    outdir = os.path.join(HERE, "build_" + tag)
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    shutil.copy(os.path.join(HERE, "probe_core_top.v"), outdir)
    for m in MEMS:
        shutil.copy(os.path.join(BRINGUP_BUILD, m), outdir)
    open(os.path.join(outdir, "synth.tcl"), "w").write(
        TCL.format(root=ROOT))
    with open(os.path.join(outdir, "vivado.log"), "w") as fh:
        rc = subprocess.run([VIVADO, "-mode", "batch", "-source",
                             "synth.tcl"], cwd=outdir,
                            stdout=fh, stderr=subprocess.STDOUT)
    log = open(os.path.join(outdir, "vivado.log")).read()
    for key in ("DSP48", "CARRY8", "RAMB18", "RAMB36"):
        m = re.search(key + r": (\d+)", log)
        print(key + ":", m.group(1) if m else "?")
    trpt = open(os.path.join(outdir, "timing.rpt")).read()
    met = "All user specified timing constraints are met." in trpt
    print("TIMING:", "MET" if met else "VIOLATED")
    wns = re.search(r"^\s+(-?[\d.]+)\s+-?[\d.]+\s+(\d+)\s+\d+\s+",
                    trpt, re.M)
    if wns:
        print("WNS: %s  TNS: %s  failing endpoints: %s"
              % (wns.group(1), wns.group(2), ""))
    src = re.findall(r"^  Source:\s+(\S+)", trpt, re.M)
    dst = re.findall(r"^  Destination:\s+(\S+)", trpt, re.M)
    lv = re.findall(r"Logic Levels:\s+(\d+)\s+\(([^)]*)\)", trpt)
    for i in range(min(6, len(src))):
        print("  path: %s -> %s  levels %s" % (src[i], dst[i], lv[i][0]))
    util = open(os.path.join(outdir, "utilization.rpt")).read()
    for pat, name in ((r"CLB LUTs\*\s+\|\s*(\d+)", "LUT"),
                      (r"CLB Registers\s+\|\s*(\d+)", "FF"),
                      (r"CARRY8\s+\|\s*(\d+)", "CARRY8(u)")):
        m = re.search(pat, util)
        print(name + ":", m.group(1) if m else "?")
    return met


if __name__ == "__main__":
    run()
