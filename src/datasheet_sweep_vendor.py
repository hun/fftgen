#!/usr/bin/env python3
"""Sparse cross-vendor datasheet sweep: Cyclone V (Quartus) / ECP5 (LSE).

For each configuration (N, R, arch) this exports the core
(export_core), synthesizes with the vendor flow (src.synth_vendor), and
writes a markdown table to the given path:

    python3 -m src.datasheet_sweep_vendor intel doc/datasheet_cyclone5.md
    python3 -m src.datasheet_sweep_vendor lattice doc/datasheet_ecp5.md

Configs: N in {16, 64, 256} R=1 plus one SSR row (N=128 R=2), for each
arch (r2 and r22, --arch). All: 16-bit samples / 18-bit twiddles, auto
scaling (the datasheet baseline widths). Results are cached per config
in <jobs-dir>/N<N>_R<R>_<arch>_<tool>/result.json. The KU5P Vivado
datasheet (doc/datasheet.md) remains the timing reference; these tables
document DSP/ALM/LUT/RAM scaling on the other two families.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))


def export_cfg(n, ssr, arch, outdir):
    cmd = ["python3", "-m", "src.export_core", "--num-points", str(n),
           "--stage-mode", arch, "--outdir", outdir]
    if ssr > 1:
        cmd += ["--ssr", str(ssr), "--output-order", "native"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"export N={n} ssr={ssr} failed:\n{r.stderr[-800:]}")


def synth_cfg(tool, n, ssr, arch, part, base):
    outdir = os.path.join(base, f"N{n}_R{ssr}_{arch}_{tool[:3]}")
    cache = os.path.join(outdir, "result.json")
    if os.path.isfile(cache):
        with open(cache) as f:
            return json.load(f)
    export_cfg(n, ssr, arch, outdir)
    cmd = ["python3", "-m", "src.synth_vendor", tool, outdir]
    if part:
        cmd.append(part)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        row = {"N": n, "R": ssr, "arch": arch, "error": "FAIL",
               "log": (r.stdout + "\n" + r.stderr)[-200:]}
    else:
        line = r.stdout.strip().splitlines()[-1]
        kv = {}
        for tok in line.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv[k] = v
        kv.update({"N": n, "R": ssr, "arch": arch})
        row = kv
    os.makedirs(outdir, exist_ok=True)
    with open(cache, "w") as f:
        json.dump(row, f)
    return row


def write_md(tool, path, part, rows):
    title = {"intel": "Cyclone V (Quartus 25.1 Lite)",
             "lattice": "ECP5 (Lattice Diamond 3.14 LSE)"}[tool]
    cols = ["N", "R", "arch", "DSP", "ALM" if tool == "intel" else "LUT4",
            "regs", "M10K" if tool == "intel" else "EBR"]
    with open(path, "w") as f:
        f.write(f"# fftgen datasheet -- {title}\n\n")
        f.write(f"{part}, synthesis only (no P&R timing), "
                f"16-bit samples / 18-bit twiddles (Q17), auto scaling, "
                f"PIPE_DEPTH=10. DSP counts are inferred multipliers "
                f"(all multiplies on DSP blocks, none in fabric). "
                f"Sweep generated {datetime.date.today().isoformat()}.\n\n")
        f.write("| " + " | ".join(cols) + " |\n")
        f.write("|" + "---|" * len(cols) + "\n")
        for row in rows:
            if "error" in row:
                f.write(f"| {row['N']} | {row['R']} | {row['arch']} | "
                        f"FAIL | | | |\n")
                continue
            f.write("| " + " | ".join(
                str(row.get(c, "?")) for c in cols) + " |\n")
        f.write("\n")
    print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tool", choices=("intel", "lattice"))
    ap.add_argument("out_md")
    ap.add_argument("--part", default=None)
    ap.add_argument("--sizes", nargs="+", type=int, default=[16, 64, 256])
    ap.add_argument("--arch", choices=("r2", "r22", "both"), default="both")
    ap.add_argument("--ssr-row", action="store_true", default=True)
    ap.add_argument("--jobs-dir", default="build/synth_vendor_ds")
    args = ap.parse_args()
    os.makedirs(args.jobs_dir, exist_ok=True)

    archs = ("r2", "r22") if args.arch == "both" else (args.arch,)
    rows = []
    for arch in archs:
        for n in args.sizes:
            rows.append(synth_cfg(args.tool, n, 1, arch, args.part,
                                  args.jobs_dir))
        if args.ssr_row:
            rows.append(synth_cfg(args.tool, 128, 2, arch, args.part,
                                  args.jobs_dir))
    rows.sort(key=lambda r: (r.get("N", 0), r.get("R", 0),
                             r.get("arch", "")))
    write_md(args.tool, args.out_md, args.part, rows)


if __name__ == "__main__":
    main()
