#!/bin/bash
# S1 spike runner: OOC-synthesize one coding/part/geometry combination.
# usage: ./run_one.sh <part> <coding> <depth> <width>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VIVADO="${VIVADO_BIN:-/tools/Xilinx/2026.1/Vivado/bin/vivado}"

part=$1; coding=$2; depth=$3; width=$4

case "$coding" in
    sdp)      rtl="$HERE/rtl/ram_sdp.v";      top=ram_sdp ;;
    rw1)      rtl="$HERE/rtl/ram_rw1.v";      top=ram_rw1 ;;
    tdp_pair) rtl="$HERE/rtl/ram_tdp_pair.v"; top=ram_tdp_pair ;;
    sdp_deep) rtl="$HERE/rtl/ram_sdp_deep.v"; top=ram_sdp_deep ;;
    *) echo "unknown coding $coding" >&2; exit 2 ;;
esac

outdir="$HERE/build/${coding}_${depth}x${width}_${part}"
mkdir -p "$outdir"
echo ">>> $coding ${depth}x${width} @ $part"

"$VIVADO" -mode batch -nojournal -nolog \
    -source "$HERE/tcl/synth.tcl" -tclargs "$part" "$rtl" "$top" "$outdir" "$depth" "$width" \
    > "$outdir/vivado.log" 2>&1 || { echo "FAILED (see $outdir/vivado.log tail:)"; tail -20 "$outdir/vivado.log"; exit 1; }

echo "--- summary ---"
cat "$outdir/summary.txt"
