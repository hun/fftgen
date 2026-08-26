#!/bin/bash
# SSR R=2 sweep: OOC synth + post-synth timing on KU5P @ 2.0 ns.
# Reuses generator artifacts from build/r2sweep/N*_R2 (functional sweep).
# usage: ./run_r2_sweep.sh N [N ...]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VIVADO="${VIVADO_BIN:-/tools/Xilinx/2026.1/Vivado/bin/vivado}"
PART="xcku5p-ffva676-1-e"

for N in "$@"; do
    srcdir="$HERE/../../build/r2sweep/N${N}_R2"
    [ -d "$srcdir" ] || srcdir="$HERE/../../build/ssr/N${N}_R2"
    outdir="$HERE/build/r2sweep_N${N}"
    mkdir -p "$outdir"
    cp "$srcdir/fft_wn.mem" "$srcdir/fft_twiddles_lane.mem" \
       "$srcdir/fft_preloads.vh" "$outdir/"
    python3 - "$N" > "$outdir/params.txt" <<'PY'
import sys
sys.path.insert(0, "/home/hannes/Projects/fftgen/src")
from config import FFTConfig
import dataclasses
cfg = FFTConfig(num_points=int(sys.argv[1]), ssr=2, output_order="native")
lane = dataclasses.replace(cfg, num_points=cfg.num_points // 2, ssr=1)
pack = sum((sh & 3) << (2 * s_) for s_, sh in enumerate(lane.shifts))
intern = cfg.sample_width + max(0, lane.num_stages - sum(lane.shifts)) + 1
print(pack, intern)
PY
    read PACK INTERN < "$outdir/params.txt"
    echo ">>> SSR R=2 N=$N (lane pack=$PACK intern=$INTERN) KU5P @ 2.0ns"
    (cd "$outdir" && "$VIVADO" -mode batch -nojournal -nolog \
        -source "$HERE/tcl/ssr_synth_ku5p.tcl" \
        -tclargs "$PART" "$N" 2 "$PACK" "$INTERN" . \
        > vivado.log 2>&1) || { echo "FAILED:"; tail -15 "$outdir/vivado.log"; continue; }
    grep -E "Slice LUTs|LUT as Memory|Block RAM Tile|DSPs|URAM " \
        "$outdir/util.txt" | grep -v Note || true
    grep -A6 "Design Timing Summary" "$outdir/timing.txt" | tail -1
done
