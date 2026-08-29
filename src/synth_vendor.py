#!/usr/bin/env python3
"""Cross-vendor synthesis driver: Quartus (Cyclone V) and Lattice (ECP5).

Takes an fftgen EXPORT directory (python3 -m src.export_core --outdir ...),
sets up the vendor project, runs synthesis + fit/map, and prints the
resource summary as one ``key=value`` line:

    python3 -m src.synth_vendor intel   <export_dir> [part]
    python3 -m src.synth_vendor lattice <export_dir> [part]

The export must have the parameterized top baked (R=1: fft_core.v; SSR:
fft_ssr.v / fft_ssr_r22.v / fft_ssr_r22_inv.v -- the export bakes these
since the portability fix; see export_core.bake_ssr_wrapper).

Tool paths come from the environment (QUARTUS_BIN, DIAMOND_ROOT); the
defaults point at the shared /tools installs. Licenses are inherited
from the caller's environment -- never baked into this script.
"""
import os
import re
import shutil
import subprocess
import sys

QUARTUS_BIN = os.environ.get("QUARTUS_BIN",
                             "/tools/Altera/altera_lite/25.1std/quartus/bin")
DIAMOND_ROOT = os.environ.get("DIAMOND_ROOT", "/tools/lscc/diamond/3.14")

# ----------------------------------------------------------------------
# shared: find the top entity in an export
# ----------------------------------------------------------------------

TOPS = ("fft_core.v", "fft_ssr.v", "fft_ssr_r22.v", "fft_ssr_r22_inv.v",
        "fft_top.v")


def find_top(export_dir):
    for wrap in TOPS:
        p = os.path.join(export_dir, wrap)
        if os.path.isfile(p):
            m = re.search(r"^module\s+([A-Za-z0-9_]+)", open(p).read(),
                          re.M)
            if m:
                return m.group(1)
    raise SystemExit(f"no top wrapper found in {export_dir}")


# ----------------------------------------------------------------------
# Intel / Quartus (Cyclone V)
# ----------------------------------------------------------------------

def run_intel(export_dir, part="5CEBA7F23C7"):
    export_dir = os.path.abspath(export_dir)
    qdir = os.path.join(export_dir, "quartus")
    shutil.rmtree(qdir, ignore_errors=True)
    os.makedirs(qdir)
    for fn in os.listdir(export_dir):
        if fn.endswith((".v", ".vh", ".mem")):
            shutil.copy(os.path.join(export_dir, fn), os.path.join(qdir, fn))
    top = find_top(export_dir)
    name = "fft_intel"
    with open(os.path.join(qdir, name + ".qsf"), "w") as f:
        f.write(f'set_global_assignment -name FAMILY "Cyclone V"\n'
                f"set_global_assignment -name DEVICE {part}\n"
                f"set_global_assignment -name TOP_LEVEL_ENTITY {top}\n"
                f"set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output\n"
                f"set_global_assignment -name SEARCH_PATH .\n")
        for fn in sorted(os.listdir(export_dir)):
            if fn.endswith(".v"):
                f.write(f"set_global_assignment -name VERILOG_FILE {fn}\n")
    with open(os.path.join(qdir, name + ".qpf"), "w") as f:
        f.write(f'QUARTUS_VERSION = "25.1"\nPROJECT_REVISION = "{name}"\n')

    def run(tool, *args):
        r = subprocess.run([os.path.join(QUARTUS_BIN, tool), *args],
                           cwd=qdir, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"{tool} failed:\n{r.stderr[-1500:]}")

    run("quartus_map", "--read_settings_files=on", name)
    run("quartus_fit", "--read_settings_files=on", name)
    summ = os.path.join(qdir, "output", f"{name}.fit.summary")
    text = open(summ).read()

    def grab(key):
        m = re.search(rf"(?m)^{re.escape(key)}\s*:\s*([^\n]+)", text)
        return m.group(1).strip() if m else "?"

    print(f"tool=intel part={part} top={top} "
          f"DSP={grab('Total DSP Blocks')} "
          f"ALM={grab('Logic utilization (in ALMs)')} "
          f"regs={grab('Total registers')} "
          f"M10K={grab('Total RAM Blocks')}")


# ----------------------------------------------------------------------
# Lattice / LSE (ECP5)
# ----------------------------------------------------------------------

def diamond_env():
    env = dict(os.environ)
    env["FOUNDRY"] = os.path.join(DIAMOND_ROOT, "ispfpga")
    env["LD_LIBRARY_PATH"] = os.path.join(DIAMOND_ROOT, "ispfpga", "bin",
                                          "lin64") + ":" + \
        os.path.join(DIAMOND_ROOT, "bin", "lin64") + ":" + \
        os.path.join(DIAMOND_ROOT, "tcltk", "lib") + ":" + \
        env.get("LD_LIBRARY_PATH", "")
    return env


def run_lattice(export_dir, part="LFE5U-85F-8BG756C"):
    export_dir = os.path.abspath(export_dir)
    # device "LFE5U-<size>-<speed><pkg>" -> map args
    m = re.match(r"LFE5U-([A-Z0-9]+)-(\d)([A-Z0-9]+)", part)
    if not m:
        raise SystemExit(f"cannot parse ECP5 part {part!r}")
    size, speed, pkg = m.groups()
    # "BG756C" -> pin count "756"; the map tool wants the full "CABGA756"
    pm = re.search(r"(\d+)", pkg)
    if not pm:
        raise SystemExit(f"cannot parse ECP5 package {pkg!r}")
    pin = pm.group(1)
    map_args = ["-a", "ECP5U", "-p", f"LFE5U-{size}", "-t", f"CABGA{pin}",
                "-s", speed]
    env = diamond_env()

    ldir = os.path.join(export_dir, "lattice")
    shutil.rmtree(ldir, ignore_errors=True)
    os.makedirs(ldir)
    for fn in os.listdir(export_dir):
        if fn.endswith((".v", ".vh", ".mem")):
            shutil.copy(os.path.join(export_dir, fn), os.path.join(ldir, fn))
    top = find_top(export_dir)
    # r22 exports include fft_sdf.v (provides fft_stage for the
    # leftover) which references fft_twiddles.mem via $readmemh; LSE
    # fatals on missing files even for non-instantiated modules.
    if os.path.isfile(os.path.join(ldir, "fft_sdf.v")) and \
       not os.path.isfile(os.path.join(ldir, "fft_twiddles.mem")):
        open(os.path.join(ldir, "fft_twiddles.mem"), "w").close()
    lpf = os.path.join(ldir, "fft_ecp5.lpf")
    with open(lpf, "w") as f:
        f.write("# fftgen ECP5 testrun: no constraints (inference check)\n")

    # 1. diamondc creates the project + LSE synproj; inject -top before
    #    synthesis so LSE picks the correct top module
    tcl = os.path.join(ldir, "proj.tcl")
    with open(tcl, "w") as f:
        f.write(f"prj_project new -name fft_ecp5 -impl impl1 -dev {part}\n")
        files = " ".join(sorted(fn for fn in os.listdir(ldir)
                                if fn.endswith(".v")))
        f.write(f"prj_src add -format VERILOG -include . {files}\n")
        f.write("prj_project save\n")
        f.write("prj_run Synthesis -impl impl1\n")
    r = subprocess.run([os.path.join(DIAMOND_ROOT, "bin", "lin64",
                                     "diamondc"), tcl],
                       cwd=ldir, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"diamondc failed:\n{r.stdout[-1500:]}\n"
                         f"{r.stderr[-1500:]}")
    synproj = os.path.join(ldir, "impl1", "fft_ecp5_impl1_lattice.synproj")
    if not os.path.isfile(synproj):
        raise SystemExit(f"synproj not generated: {synproj}")

    # 2. inject the top and run LSE + map directly (diamondc's auto-top
    #    heuristic picks the wrong module for generate-heavy designs)
    text = open(synproj).read()
    if "-top " not in text:
        text = text.replace("-use_io_insertion 1",
                            f"-top {top}\n-use_io_insertion 1", 1)
    open(synproj, "w").write(text)

    impl = os.path.join(ldir, "impl1")
    ipfpga = os.path.join(DIAMOND_ROOT, "ispfpga", "bin", "lin64")
    r = subprocess.run([os.path.join(ipfpga, "synthesis"), "-f",
                        os.path.basename(synproj)], cwd=impl, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"LSE synthesis failed:\n{r.stdout[-1500:]}")
    ngd = os.path.join(impl, "fft_ecp5_impl1.ngd")
    if not os.path.isfile(ngd):
        raise SystemExit("LSE produced no .ngd")
    r = subprocess.run([os.path.join(ipfpga, "map"), *map_args, "-oc",
                        "Commercial", "fft_ecp5_impl1.ngd", "-o",
                        "fft_ecp5_impl1_map.ncd", "-pr",
                        "fft_ecp5_impl1.prf", "-mp",
                        "fft_ecp5_impl1.mrp", "-lpf",
                        lpf],
                       cwd=impl, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"map failed:\n{r.stdout[-1500:]}")
    mrp = os.path.join(impl, "fft_ecp5_impl1.mrp")
    text = open(mrp).read()

    def grab(key):
        m2 = re.search(rf"{key}\s*:?\s*(\d+)", text)
        return m2.group(1) if m2 else "?"

    # LSE's retiming shadows each logical multiplier with a lat_mult_*
    # copy (same multiply, retimed into a second DSP block); the mrp
    # total double-counts r22 designs. Count the mult_* instances only
    # (lat_* excluded) -- verified 1:1 against Vivado DSP48E2 counts
    # (r2 N16: 8/8, r22 N16: 8/8, r22 N64: 12/12).
    # LSE's retiming shadows each logical r22 multiplier with a
    # lat_mult_* copy (same multiply retimed into a second DSP block);
    # the mrp total double-counts. Exclude lat_mult_* instances --
    # verified 1:1 against Vivado DSP48E2 for every sweep row:
    # r2 8/16/24/36, r22 8/12/16/28 (crossbar _37xx instances are real
    # multipliers and must stay).
    insts = set(re.findall(r"\. MULT18X18D\s+(\S+):", text))
    muls = [i for i in insts if "/lat_mult_" not in i]
    dsp = len(muls) if muls else grab("MULT18X18D")

    print(f"tool=lattice part={part} top={top} "
          f"DSP={dsp} "
          f"LUT4={grab('Number of LUT4s')} "
          f"regs={grab('Number of registers')} "
          f"EBR={grab('Number of block RAMs')} "
          f"SLICE_RAM={grab('SLICEs as RAM')}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print(__doc__)
        return 1
    tool, export_dir = argv[0], argv[1]
    part = argv[2] if len(argv) > 2 else None
    if tool == "intel":
        run_intel(export_dir, part or "5CEBA7F23C7")
    elif tool == "lattice":
        run_lattice(export_dir, part or "LFE5U-85F-8BG756C")
    else:
        raise SystemExit(f"unknown tool {tool!r} (intel|lattice)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
