#!/bin/bash
# build + run the corner-inverse export sim, exactly as README.txt says
set -e
cd "$(dirname "$0")"
verilator --cc --exe --build -j 4 --top-module fft_ssr_r22_inv \
  -Wno-fatal \
  -CFLAGS "-DTB_SAMPLE_WIDTH=16 -DTB_OUTPUT_WIDTH=16 -DTB_SSR=2" \
  -GNUM_POINTS=2048 -GSSR=2 \
  -GSCALING_PACK=32\'h00055555 -GINTERN_WIDTH=17 \
  -GINVERSE=1 -GPIPE_DEPTH=10 \
  fft_ssr_r22_inv.v fft_top_r22.v fft_sdf_r22.v fft_stage_r22.v \
  fft_sdf.v fft_reorder.v tb/tb_fft_ssr_r22_inv.cpp
./obj_dir/Vfft_ssr_r22_inv
