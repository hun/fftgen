#!/bin/bash
# R=1 scaling sweep: OOC synth + post-synth timing on KU5P @ 2.0 ns.
# usage: ./run_r1_sweep.sh N [N ...]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VIVADO="${VIVADO_BIN:-/tools/Xilinx/2026.1/Vivado/bin/vivado}"
PART="xcku5p-ffva676-1-e"
CLK_NS=2.0

for N in "$@"; do
    outdir="$HERE/build/r1sweep_N${N}"
    mkdir -p "$outdir"
    # generator artifacts (twiddles + preload pack + generics)
    (cd "$outdir" && python3 - "$N" <<'PY'
import sys
sys.path.insert(0, "/home/hannes/Projects/fftgen/src")
from config import FFTConfig
from fft_gen import write_twiddle_mem
N = int(sys.argv[1])
cfg = FFTConfig(num_points=N)
pack = 0
for s, sh in enumerate(cfg.shifts):
    pack |= (sh & 3) << (2*s)
intern = cfg.sample_width + max(0, cfg.num_stages - sum(cfg.shifts)) + 1
write_twiddle_mem(cfg, "fft_twiddles.mem")
with open("params.txt", "w") as f:
    f.write(f"{pack} {intern} {cfg.num_stages}\n")
PY
    )
    read PACK INTERN NSTAGES < "$outdir/params.txt"
    cp "$HERE/../../rtl/fft_sdf.v" "$outdir/"
    echo ">>> R=1 N=$N (stages=$NSTAGES, intern=$INTERN) KU5P @ ${CLK_NS}ns"
    (cd "$outdir" && "$VIVADO" -mode batch -nojournal -nolog \
        -source "$HERE/tcl/synth.tcl" \
        -tclargs "$PART" "$N" 16 18 "$PACK" "$INTERN" . "$CLK_NS" \
        > vivado.log 2>&1) || { echo "FAILED:"; tail -15 "$outdir/vivado.log"; continue; }
    grep -E "^PART|LUT as Memory|Slice LUTs|Slice Registers|Block RAM Tile|DSPs|URAM|^\s*clk\s+-\||WNS" \
        "$outdir/util.txt" "$outdir/timing.txt" 2>/dev/null | sed 's/.*txt://' | head -10 || true
done
