"""S7 RTL probe: synthesize ONE fft_stage_r23 (G=128, 16b/18b) OOC on
KU5P at 2 ns; report WNS, DSP48 (must be 4 -- the rot trees must stay
fabric), CARRY8, LUT, and the worst paths."""
import os
import re
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
VIVADO = "/tools/Xilinx/2026.1/Vivado/bin/vivado"

TCL = """
create_project -in_memory -part xcku5p-ffva676-1-e
read_verilog {root}/rtl/fft_stage_r23.v
read_verilog probe_top.v
synth_design -top fft_probe_top -part xcku5p-ffva676-1-e
create_clock -period 2.000 -name clk [get_ports clk]
report_utilization -file utilization.rpt
report_timing_summary -delay_type max -max_paths 8 -nworst 2 -file timing.rpt
set dsps [get_cells -hier -filter {{REF_NAME == DSP48E2}}]
puts "DSP48: [llength $dsps]"
puts "CARRY8: [llength [get_cells -hier -filter {{REF_NAME == CARRY8}}]]"
exit
"""


def run(tag="probe"):
    outdir = os.path.join(HERE, "build_" + tag)
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    shutil.copy(os.path.join(HERE, "probe_top.v"), outdir)
    # generate the twiddle mem into the build dir
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gen_mem", os.path.join(HERE, "gen_mem.py"))
    # gen_mem writes to build_probe; run it via subprocess with cwd trick
    r = subprocess.run([os.sys.executable,
                        os.path.join(HERE, "gen_mem.py")],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    open(os.path.join(outdir, "synth.tcl"), "w").write(
        TCL.format(root=ROOT))
    with open(os.path.join(outdir, "vivado.log"), "w") as fh:
        rc = subprocess.run([VIVADO, "-mode", "batch", "-source",
                             "synth.tcl"], cwd=outdir,
                            stdout=fh, stderr=subprocess.STDOUT)
    log = open(os.path.join(outdir, "vivado.log")).read()
    dsp = re.search(r"DSP48: (\d+)", log)
    c8 = re.search(r"CARRY8: (\d+)", log)
    trpt = open(os.path.join(outdir, "timing.rpt")).read()
    met = "All user specified timing constraints are met." in trpt
    wns = re.search(r"^\s+(-?[\d.]+)\s+-?[\d.]+\s+(\d+)\s+\d+\s+",
                    trpt, re.M)
    src = re.findall(r"^  Source:\s+(\S+)", trpt, re.M)
    dst = re.findall(r"^  Destination:\s+(\S+)", trpt, re.M)
    lv = re.findall(r"Logic Levels:\s+(\d+)\s+\(([^)]*)\)", trpt)
    util = open(os.path.join(outdir, "utilization.rpt")).read()
    lut = re.search(r"CLB LUTs\*\s+\|\s*(\d+)", util)
    ff = re.search(r"CLB Registers\s+\|\s*(\d+)", util)
    print(f"[{tag}] rc={rc.returncode} DSP={dsp.group(1) if dsp else '?'} "
          f"CARRY8={c8.group(1) if c8 else '?'} "
          f"LUT={lut.group(1) if lut else '?'} "
          f"FF={ff.group(1) if ff else '?'} "
          f"WNS={'MET' if met else (wns.group(1) if wns else '?')} "
          f"failEP={0 if met else (wns.group(2) if wns else '?')}")
    for i, (s, d) in enumerate(zip(src, dst)):
        l = lv[i] if i < len(lv) else ("?", "?")
        print(f"    {l[0]} lvl ({l[1].strip()}): "
              f"{s.split('/')[-2] if '/' in s else s} -> {d.split('/')[-1]}")
    return outdir


if __name__ == "__main__":
    run()
