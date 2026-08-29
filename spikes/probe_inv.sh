#!/bin/bash
# build + run the fft_ssr_r22_inv wrapper probe (run from the generated build dir)
set -e
cd "$(dirname "$0")"
verilator --cc --exe --build -j 4 --top-module fft_ssr_r22_inv --public-flat-rw \
  -Wno-fatal \
  -CFLAGS "-DTB_SAMPLE_WIDTH=16 -DTB_OUTPUT_WIDTH=16 -DTB_SSR=2" \
  -GNUM_POINTS=8 -GSSR=2 -GSAMPLE_WIDTH=16 -GSAMPLE_DECIMAL=0 \
  -GOUTPUT_WIDTH=16 -GOUTPUT_DECIMAL=0 -GTWIDDLE_WIDTH=18 \
  -GTWIDDLE_DECIMAL=17 \
  -GINTERN_WIDTH=17 -GPIPE_DEPTH=10 -GINVERSE=1 \
  fft_ssr_r22_inv.v fft_top_r22.v fft_sdf_r22.v fft_stage_r22.v \
  fft_sdf.v fft_reorder.v /home/hannes/Projects/fftgen/spikes/probe_inv.cpp
./obj_dir/Vfft_ssr_r22_inv
