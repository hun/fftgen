#!/usr/bin/env python3
"""Sparse cross-vendor datasheet sweep: Cyclone V (Quartus) / ECP5 (LSE).

For each configuration (N, R, arch) this exports the core
(export_core), synthesizes with the vendor flow (src.synth_vendor), and
writes a markdown table to the given path:

    python3 -m src.datasheet_sweep_vendor intel doc/datasheet_cyclone5.md
    python3 -m src.datasheet_sweep_vendor lattice doc/datasheet_ecp5.md

Configs: N in {16, 64, 256} radix-2 R=1, plus one SSR row (N=128 R=2).
All: 16-bit samples / 18-bit twiddles, auto scaling (the datasheet
baseline widths). The KU5P Vivado datasheet (doc/datasheet.md) remains
the timing reference; these tables document DSP/ALM/LUT/RAM scaling on
the other two families.
"""
import argparse
import datetime
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
    outdir = os.path.join(base, f"N{n}_R{ssr}")
    export_cfg(n, ssr, arch, outdir)
    cmd = ["python3", "-m", "src.synth_vendor", tool, outdir]
    if part:
        cmd.append(part)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"N": n, "R": ssr, "arch": arch, "error": "FAIL"}
    line = r.stdout.strip().splitlines()[-1]
    kv = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            kv[k] = v
    kv.update({"N": n, "R": ssr, "arch": arch})
    return kv


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
    ap.add_argument("--ssr-row", action="store_true", default=True)
    ap.add_argument("--jobs-dir", default="build/synth_vendor_ds")
    args = ap.parse_args()
    os.makedirs(args.jobs_dir, exist_ok=True)

    rows = []
    for n in args.sizes:
        rows.append(synth_cfg(args.tool, n, 1, "r2", args.part,
                              args.jobs_dir))
    if args.ssr_row:
        rows.append(synth_cfg(args.tool, 128, 2, "r2", args.part,
                              args.jobs_dir))
    write_md(args.tool, args.out_md, args.part, rows)


if __name__ == "__main__":
    main()
