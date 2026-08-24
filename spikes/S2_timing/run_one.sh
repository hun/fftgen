#!/bin/bash
# S2 runner: OOC synth + timing for one config on KU3P.
# usage: ./run_one.sh <N> <sample_width> <twiddle_width> [clk_ns]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VIVADO="${VIVADO_BIN:-/tools/Xilinx/2026.1/Vivado/bin/vivado}"
PART="xcku3p-ffva676-1-e"

N=$1; SW=$2; TW=$3; CLK_NS=${4:-2.0}

python3 - "$N" "$SW" "$TW" <<'PY'
import sys
sys.path.insert(0, "/home/hannes/Projects/fftgen/src")
from config import FFTConfig
from fft_gen import write_twiddle_mem
N, SW, TW = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
cfg = FFTConfig(num_points=N, sample_width=SW, twiddle_width=TW)
pack = 0
for s, sh in enumerate(cfg.shifts):
    pack |= (sh & 3) << (2*s)
intern = SW + max(0, cfg.num_stages - sum(cfg.shifts)) + 1
print(f"PACK={pack} INTERN={intern}")
write_twiddle_mem(cfg, "/tmp/fft_twiddles.mem")
open("/tmp/s2_params.txt","w").write(f"{pack} {intern}\n")
PY
read PACK INTERN < /tmp/s2_params.txt
cp /tmp/fft_twiddles.mem "$HERE/fft_twiddles.mem"

outdir="$HERE/build/N${N}_sw${SW}_tw${TW}_${CLK_NS}ns"
mkdir -p "$outdir"
cp fft_sdf.v "$outdir/"
echo ">>> N=$N SW=$SW TW=$TW @ ${CLK_NS}ns (${PART})"

"$VIVADO" -mode batch -nojournal -nolog \
    -source "$HERE/tcl/synth.tcl" \
    -tclargs "$PART" "$N" "$SW" "$TW" "$PACK" "$INTERN" "$outdir" "$CLK_NS" \
    > "$outdir/vivado.log" 2>&1 || { echo "FAILED:"; tail -15 "$outdir/vivado.log"; exit 1; }

echo "--- summary ---"
cat "$outdir/summary.txt"
