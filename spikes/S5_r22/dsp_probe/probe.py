"""P7 step 7 probe: does the R2^2 pair stage map its complex multiply with
the DSP MREG engaged, or does Vivado merge the re/im combine into one ALU
pass (BREG->PREADD->MULT->ALU->PREG = 1.85 ns, misses 500 MHz)?

Synthesizes ONE fft_stage_r22 (D=128, 16b data, 18b twiddle) OOC on KU5P
at 2.0 ns from a variant file, and prints WNS + the worst-path endpoints +
the DSP48 count. The intra-DSP hop is size-independent (r2 sweep notes),
so this predicts the full-core result at ~1/4 the runtime.

Run:  python3 spikes/S5_r22/dsp_probe/probe.py [variant ...]
      VARIANT=<stage.v> env to add your own file
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPIKE = os.path.abspath(os.path.join(HERE, ".."))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
RTL = os.path.join(ROOT, "rtl")
VIVADO = "/tools/Xilinx/2026.1/Vivado/bin/vivado"

sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, SPIKE)

TCL = """
create_project -in_memory -part xcku5p-ffva676-1-e
read_verilog fft_probe_top.v
read_verilog fft_stage_r22.v
synth_design -top fft_probe_top -part xcku5p-ffva676-1-e
create_clock -period 2.000 -name clk [get_ports clk]
report_utilization -file utilization.rpt
report_timing_summary -delay_type max -max_paths 5 -nworst 2 -file timing.rpt
set dsps [get_cells -hier -filter {REF_NAME == DSP48E2}]
puts "DSP48: [llength $dsps]"
set c0 [lindex $dsps 0]
foreach p [list_properties $c0] {
    if {[string match *REG* $p] || [string match *PIPE* $p]} {
        puts "PROP $p=[get_property $p $c0]"
    }
}
# which internal cells exist (a bypassed MREG still exists, so also dump
# the reg-mode params Vivado leaves on the primitive)
foreach c [get_cells -hier -filter {NAME =~ *M_DATA* || NAME =~ *OUTPUT*}] {
    puts "CELL $c [get_property REF_NAME $c]"
}
exit
"""


def prep(stage_src, outdir):
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    shutil.copy(stage_src, os.path.join(outdir, "fft_stage_r22.v"))
    shutil.copy(os.path.join(HERE, "fft_probe_top.v"), outdir)
    from config import FFTConfig
    from rtl_check import write_r22_twiddle_mem
    write_r22_twiddle_mem(FFTConfig(num_points=512),
                          os.path.join(outdir, "fft_twiddles_r22.mem"))
    open(os.path.join(outdir, "synth.tcl"), "w").write(TCL)


def run(stage_src, tag):
    outdir = os.path.join(HERE, "build_" + tag)
    prep(stage_src, outdir)
    with open(os.path.join(outdir, "vivado.log"), "w") as fh:
        r = subprocess.run([VIVADO, "-mode", "batch", "-source", "synth.tcl"],
                           cwd=outdir, stdout=fh, stderr=subprocess.STDOUT)
    log = open(os.path.join(outdir, "vivado.log")).read()
    dsp = re.search(r"DSP48: (\d+)", log)
    wns = re.search(r"^\s+(-?[\d.]+)\s+-?[\d.]+\s+(\d+)\s+\d+\s+",
                    open(os.path.join(outdir, "timing.rpt")).read(), re.M)
    src = re.findall(r"^  Source:\s+(\S+)",
                     open(os.path.join(outdir, "timing.rpt")).read(), re.M)
    dst = re.findall(r"^  Destination:\s+(\S+)",
                     open(os.path.join(outdir, "timing.rpt")).read(), re.M)
    lv = re.findall(r"Logic Levels:\s+(\d+)\s+\(([^)]*)\)",
                    open(os.path.join(outdir, "timing.rpt")).read())
    print(f"[{tag}] rc={r.returncode} DSP={dsp.group(1) if dsp else '?'} "
          f"WNS={wns.group(1) if wns else 'MET?'} "
          f"failEP={wns.group(2) if wns else 0}")
    for i, (s, d) in enumerate(zip(src, dst)):
        l = lv[i] if i < len(lv) else ("?", "?")
        print(f"    {l[0]} lvl ({l[1].strip()}): {s.split('/')[-2] if '/' in s else s}"
              f" -> {d.split('/')[-1]}")
    return outdir


if __name__ == "__main__":
    variants = sys.argv[1:] or ["cur"]
    for v in variants:
        src = v if v.endswith(".v") else os.path.join(HERE, f"fft_stage_{v}.v")
        tag = os.path.splitext(os.path.basename(src))[0].replace("fft_stage_", "")
        run(src, tag)
