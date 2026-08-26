# fftgen datasheet

## Resource & timing -- KU5P OOC synthesis @ 500 MHz

xcku5p-ffva676-1-e, Vivado 2026.1 out-of-context synthesis,
`create_clock 2.0 ns`, 16-bit samples / 18-bit twiddles (Q17), auto
scaling schedule, PIPE_DEPTH=10 (golden NLAYERS), post-warm preload
packs applied (`FFTGEN_PRELOADS`). Post-synth estimates -- the binding
paths close post-route (see PLAN.md P5a / S3 findings).

Regenerate with:

    python3 spikes/S2_timing/datasheet_sweep.py -j 4

| N | R | LUTs | FFs | LUTRAM | DSP | BRAM36 | URAM | WNS(ns) | FEP | clk/frame |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1 | 1390 | 2739 | 696 | 24 | 1 | 0 | +0.700 | 0 | 64 |
| 128 | 1 | 1690 | 3243 | 836 | 28 | 2 | 0 | +0.700 | 0 | 128 |
| 256 | 1 | 2123 | 3755 | 1058 | 32 | 3 | 0 | +0.660 | 0 | 256 |
| 512 | 1 | 2878 | 4281 | 1448 | 36 | 4 | 0 | +0.660 | 0 | 512 |
| 1024 | 1 | 4157 | 4827 | 2176 | 40 | 5 | 0 | +0.605 | 0 | 1024 |
| 2048 | 1 | 6477 | 5430 | 3574 | 44 | 7 | 0 | +0.563 | 0 | 2048 |
| 4096 | 1 | 10868 | 6108 | 6316 | 48 | 11 | 0 | +0.201 | 0 | 4096 |
| 8192 | 1 | 19754 | 6858 | 11746 | 52 | 19 | 0 | +0.163 | 0 | 8192 |
| 64 | 2 | 2638 | 5076 | 1283 | 44 | 0 | 0 | -0.020 | 96 | 32 |
| 128 | 2 | 3017 | 5957 | 1398 | 52 | 4 | 0 | -0.020 | 96 | 64 |
| 256 | 2 | 3656 | 6972 | 1681 | 60 | 6 | 0 | -0.020 | 96 | 128 |
| 512 | 2 | 4596 | 8003 | 2134 | 68 | 8 | 0 | -0.020 | 96 | 256 |
| 1024 | 2 | 6241 | 9062 | 2931 | 76 | 10 | 0 | -0.020 | 96 | 512 |
| 2048 | 2 | 9016 | 10178 | 4418 | 84 | 14 | 0 | -0.020 | 96 | 1024 |
| 4096 | 2 | 13757 | 11392 | 7023 | 92 | 23 | 0 | -0.020 | 96 | 2048 |
| 8192 | 2 | 23098 | 12829 | 12380 | 100 | 39 | 0 | -0.020 | 96 | 4096 |
| 64 | 4 | 4527 | 7868 | 1992 | 76 | 0 | 0 | -0.020 | 288 | 16 |
| 256 | 4 | 6336 | 11771 | 2774 | 108 | 8 | 0 | -0.020 | 288 | 64 |
| 1024 | 4 | 9649 | 15874 | 4206 | 140 | 16 | 0 | -0.020 | 288 | 256 |
| 4096 | 4 | 18684 | 20215 | 8626 | 172 | 28 | 0 | -0.020 | 288 | 1024 |
| 64 | 8 | 8490 | 13866 | 3175 | 140 | 0 | 0 | -0.165 | 724 | 8 |
| 256 | 8 | 12506 | 22007 | 5243 | 204 | 0 | 0 | -0.165 | 724 | 32 |
| 1024 | 8 | 16654 | 29598 | 6769 | 268 | 24 | 0 | -0.165 | 724 | 128 |


### Reading the table

- **LUTs / FFs** -- CLB LUTs / CLB registers (post-synth, before
  opt_design trimming).
- **LUTRAM** -- LUTs carrying distributed RAM (delay lines, product
  FIFOs, twiddle ROM below the block cutoff, crossbar WN table).
- **DSP** -- R=1: exactly 4 x num_stages (4-product complex multiply +
  C-port combine per stage). SSR: R lanes x 4 x stages(N/R) plus the
  crossbar network (R-point lane DFT + pre-twiddle products).
- **clk/frame** -- clocks per output frame at steady state (= N/R;
  one sample-group per clock while ce is high).

### Timing notes

| architecture | WNS behavior | limiter |
|---|---|---|
| R=1 | +0.163 .. +0.700, degrades slowly with N | product-FIFO LUTRAM read (pointer -> memory -> out); all sizes MET |
| R=2 / R=4 | -0.020 flat, N-independent | intra-DSP cascade hop (A/B-reg -> preadd -> mult -> ALU -> PREG); measured -0.021 post-route with TNS -0.152 at R=2 N=8192 (AggressiveExplore double pass) -- skew-dominated synth estimate |
| R=8 | -0.165 flat | same cascade hop plus the sqrt(2)/2 scalar-multiply split path; not yet post-routed |

### Memory policy

Delay lines / product FIFOs follow the decided cutoffs
(doc/mem_cutoffs.md): <=1024 bits distributed, <262144 bits block
(RAMB36E2), >=262144 bits URAM288. The first URAM user would be an
N=16384 R=1 stage-0 delay line; everything in this table fits BRAM.
SSR builds add per-lane reorder buffers (2M x width) which move to
block RAM from M >= 64 (visible as the BRAM36 jumps in the R>=2 rows).
The crossbar WN ROM is deliberately distributed (async read for the
pre-twiddle multiply); at large N x R it dominates LUTRAM growth --
the one clear follow-up if larger R x N corners are needed.
