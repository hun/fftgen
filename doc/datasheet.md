# fftgen datasheet

## Resource & timing -- KU5P OOC synthesis @ 500 MHz

xcku5p-ffva676-1-e, Vivado 2026.1 out-of-context synthesis,
`create_clock 2.0 ns`, 16-bit samples / 18-bit twiddles (Q17), auto
scaling schedule, PIPE_DEPTH=10 (golden NLAYERS), post-warm preload
packs applied (`FFTGEN_PRELOADS`) for `r2`. `r22` uses the production
P7 core (`rtl/fft_sdf_r22.v` + `rtl/fft_stage_r22.v`, `K_PRELOAD` phase
alignment; `rtl/fft_ssr_r22.v` for R>1) -- the same files the export
flow ships. Post-synth estimates; `r2` R=1 and `r22` R=1 close post-route
(the `r22` N=2048 corner needs the aggressive directive recipe, see the
timing notes below and PLAN.md P5a / S3 / `spikes/S5_r22/notes.md`).

Twiddle ROM style is `auto`: block RAM from N >= 256
(doc/mem_cutoffs.md S4).

Sweep regenerated 2026-08-28 with the P6 trivial-twiddle reduction
(`r2`: `4 x (stages - 2)` DSPs/engine) and the P7 `r22` core (one
multiply per stage pair, production core). The `r22` R=1 rows changed from
the 2026-08-27 sweep because of the P7 step 7 DSP-pipeline fix (staggered
C-port pairing + natural-width operands): `r22` R=1 went from
-0.020 / 288-576 FEP to MET at every N. The R>1 rows (both arches) changed
with the same fix applied to the shared crossbar (P7 step 8): every R=2/R=4
row was pinned at -0.020 / 96 FEP by the crossbar's intra-DSP hop, and now
meets 500 MHz through N=2048 (R=2) / N=4096 (R=4) -- see the timing notes
for what the largest sizes trade instead. Regenerate with:

    python3 -m src.datasheet_sweep -j 4            # both arches, all N×R
    python3 -m src.datasheet_sweep --arch r2 -j 4  # r2 only (legacy)

Legacy spike sweep remains at `spikes/S2_timing/datasheet_sweep.py`.

| N | R | Arch | LUTs | FFs | LUTRAM | DSP | BRAM36 | URAM | WNS(ns) | FEP | clk/frame |
|---:|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1 | r2 | 1848 | 3031 | 766 | 16 | 1 | 0 | +0.107 | 0 | 64 |
| 64 | 1 | r22 | 1564 | 1991 | 384 | 12 | 1.5 | 0 | +0.187 | 0 | 64 |
| 64 | 2 | r2 | 3556 | 5600 | 1437 | 28 | 0 | 0 | +0.015 | 0 | 32 |
| 64 | 2 | r22 | 3207 | 4014 | 1053 | 20 | 2 | 0 | +0.015 | 0 | 32 |
| 64 | 2 | r22b | 3099 | 3920 | 973 | 20 | 2 | 0 | +0.015 | 0 | 32 |
| 64 | 4 | r2 | 6360 | 8944 | 2290 | 44 | 0 | 0 | +0.013 | 0 | 16 |
| 64 | 4 | r22 | 4976 | 6449 | 980 | 44 | 4 | 0 | +0.013 | 0 | 16 |
| 64 | 8 | r2 | 12195 | 16046 | 3753 | 76 | 0 | 0 | -0.165 | 52 | 8 |
| 64 | 8 | r22 | 10377 | 12758 | 2847 | 76 | 4 | 0 | -0.165 | 52 | 8 |
| 128 | 1 | r2 | 2149 | 3535 | 906 | 20 | 2 | 0 | +0.107 | 0 | 128 |
| 128 | 1 | r22 | 2004 | 2362 | 660 | 12 | 1.5 | 0 | +0.187 | 0 | 128 |
| 128 | 2 | r2 | 3933 | 6513 | 1556 | 36 | 4 | 0 | +0.107 | 0 | 64 |
| 128 | 2 | r22 | 3372 | 4436 | 794 | 28 | 5 | 0 | +0.187 | 0 | 64 |
| 128 | 2 | r22b | 3338 | 4434 | 786 | 28 | 3 | 0 | +0.015 | 0 | 64 |
| 256 | 1 | r2 | 2529 | 4011 | 1128 | 24 | 3.5 | 0 | +0.107 | 0 | 256 |
| 256 | 1 | r22 | 2204 | 2624 | 648 | 16 | 2 | 0 | +0.187 | 0 | 256 |
| 256 | 2 | r2 | 4592 | 7528 | 1839 | 44 | 6 | 0 | +0.015 | 0 | 128 |
| 256 | 2 | r22 | 4316 | 5184 | 1351 | 28 | 5 | 0 | +0.015 | 0 | 128 |
| 256 | 2 | r22b | 4263 | 5178 | 1335 | 28 | 3 | 0 | +0.015 | 0 | 128 |
| 256 | 4 | r2 | 8182 | 12911 | 3072 | 76 | 8 | 0 | +0.013 | 0 | 64 |
| 256 | 4 | r22 | 7044 | 8760 | 1550 | 60 | 10 | 0 | +0.013 | 0 | 64 |
| 256 | 8 | r2 | 16182 | 24194 | 5821 | 140 | 0 | 0 | -0.165 | 52 | 32 |
| 256 | 8 | r22 | 14777 | 17850 | 4287 | 108 | 8 | 0 | -0.165 | 52 | 32 |
| 512 | 1 | r2 | 3144 | 4501 | 1518 | 28 | 5 | 0 | +0.107 | 0 | 512 |
| 512 | 1 | r22 | 2962 | 3017 | 1184 | 16 | 2 | 0 | +0.187 | 0 | 512 |
| 512 | 2 | r2 | 5414 | 8487 | 2292 | 52 | 9 | 0 | +0.015 | 0 | 256 |
| 512 | 2 | r22 | 4773 | 5716 | 1334 | 36 | 6 | 0 | +0.015 | 0 | 256 |
| 512 | 2 | r22b | 4698 | 5706 | 1302 | 36 | 4 | 0 | +0.015 | 0 | 256 |
| 1024 | 1 | r2 | 4201 | 5011 | 2246 | 32 | 7 | 0 | +0.113 | 0 | 1024 |
| 1024 | 1 | r22 | 3533 | 3240 | 1492 | 20 | 4.5 | 0 | +0.082 | 0 | 1024 |
| 1024 | 2 | r2 | 6796 | 9475 | 3089 | 60 | 12 | 0 | +0.015 | 0 | 512 |
| 1024 | 2 | r22 | 6437 | 6514 | 2425 | 36 | 6 | 0 | +0.015 | 0 | 512 |
| 1024 | 2 | r22b | 6284 | 6495 | 2359 | 36 | 4 | 0 | +0.015 | 0 | 512 |
| 1024 | 4 | r2 | 11204 | 16870 | 4504 | 108 | 18 | 0 | +0.013 | 0 | 256 |
| 1024 | 4 | r22 | 9900 | 11326 | 2590 | 76 | 12 | 0 | +0.013 | 0 | 256 |
| 1024 | 8 | r2 | 20346 | 31907 | 7347 | 204 | 24 | 0 | -0.165 | 80 | 128 |
| 1024 | 8 | r22 | 19233 | 22531 | 5401 | 140 | 20 | 0 | -0.165 | 80 | 128 |
| 2048 | 1 | r2 | 6120 | 5564 | 3644 | 36 | 10.5 | 0 | +0.113 | 0 | 2048 |
| 2048 | 1 | r22 | 5251 | 3677 | 2872 | 20 | 7.5 | 0 | +0.082 | 0 | 2048 |
| 2048 | 2 | r2 | 9129 | 10518 | 4576 | 68 | 17 | 0 | +0.015 | 0 | 1024 |
| 2048 | 2 | r22 | 7790 | 6977 | 3072 | 44 | 12 | 0 | +0.015 | 0 | 1024 |
| 2048 | 2 | r22b | 7585 | 6961 | 2944 | 44 | 8 | 0 | +0.015 | 0 | 1024 |
| 4096 | 1 | r2 | 9830 | 6186 | 6386 | 40 | 17.5 | 0 | +0.113 | 0 | 4096 |
| 4096 | 1 | r22 | 7639 | 3963 | 4868 | 24 | 15 | 0 | +0.082 | 0 | 4096 |
| 4096 | 2 | r2 | 13084 | 11640 | 7181 | 76 | 30 | 0 | -0.095 | 11 | 2048 |
| 4096 | 2 | r22 | 11376 | 7890 | 5643 | 44 | 22 | 0 | -0.095 | 11 | 2048 |
| 4096 | 2 | r22b | 11349 | 7878 | 5643 | 44 | 13 | 0 | -0.095 | 11 | 2048 |
| 4096 | 4 | r2 | 18900 | 20936 | 8924 | 140 | 36 | 0 | +0.013 | 0 | 1024 |
| 4096 | 4 | r22 | 16236 | 13888 | 5920 | 92 | 26 | 0 | +0.013 | 0 | 1024 |
| 4096 | 8 | r2 | 29737 | 39703 | 12125 | 268 | 48 | 0 | -0.165 | 52 | 512 |
| 4096 | 8 | r22 | 28345 | 27907 | 9475 | 172 | 24 | 0 | -0.165 | 52 | 512 |
| 8192 | 1 | r2 | 16692 | 6729 | 11816 | 44 | 32.5 | 0 | +0.113 | 0 | 8192 |
| 8192 | 1 | r22 | 13238 | 4540 | 9624 | 24 | 31.5 | 0 | +0.048 | 0 | 8192 |
| 8192 | 2 | r2 | 21054 | 12965 | 12538 | 84 | 52 | 0 | -0.144 | 30 | 4096 |
| 8192 | 2 | r22 | 16671 | 8500 | 9506 | 52 | 47 | 0 | -0.144 | 30 | 4096 |
| 8192 | 2 | r22b | 16627 | 8397 | 9506 | 52 | 30 | 0 | -0.098 | 12 | 4096 |
| 8192 | 4 | r2 | 27390 | 23172 | 14197 | 156 | 59 | 0 | -0.095 | 11 | 2048 |
| 8192 | 4 | r22 | 23969 | 15663 | 11125 | 92 | 47 | 0 | -0.095 | 11 | 2048 |
| 8192 | 8 | r2 | 39509 | 43854 | 17776 | 300 | 72 | 0 | -0.165 | 52 | 1024 |
| 8192 | 8 | r22 | 34191 | 29716 | 11772 | 204 | 52 | 0 | -0.165 | 52 | 1024 |

## R=1 comparison: r2 vs r22

| N | R | R2 DSP | R22 DSP | ΔDSP | R2 LUTs | R22 LUTs | ΔLUT | R2 WNS | R22 WNS | R2 BRAM | R22 BRAM |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1 | 16 | 12 | -4 | 1848 | 1564 | -284 | +0.107 | +0.187 | 1 | 1.5 |
| 128 | 1 | 20 | 12 | -8 | 2149 | 2004 | -145 | +0.107 | +0.187 | 2 | 1.5 |
| 256 | 1 | 24 | 16 | -8 | 2529 | 2204 | -325 | +0.107 | +0.187 | 3.5 | 2 |
| 512 | 1 | 28 | 16 | -12 | 3144 | 2962 | -182 | +0.107 | +0.187 | 5 | 2 |
| 1024 | 1 | 32 | 20 | -12 | 4201 | 3533 | -668 | +0.113 | +0.082 | 7 | 4.5 |
| 2048 | 1 | 36 | 20 | -16 | 6120 | 5251 | -869 | +0.113 | +0.082 | 10.5 | 7.5 |
| 4096 | 1 | 40 | 24 | -16 | 9830 | 7639 | -2191 | +0.113 | +0.082 | 17.5 | 15 |
| 8192 | 1 | 44 | 24 | -20 | 16692 | 13238 | -3454 | +0.113 | +0.048 | 32.5 | 31.5 |


## R=2 (SSR) comparison: r2 vs r22

| N | R | R2 DSP | R22 DSP | ΔDSP | R2 LUTs | R22 LUTs | ΔLUT | R2 WNS | R22 WNS | R2 BRAM | R22 BRAM |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 2 | 28 | 20 | -8 | 3553 | 3204 | -349 | +0.015 | +0.015 | 0 | 2 |
| 128 | 2 | 36 | 28 | -8 | 3930 | 3369 | -561 | +0.107 | +0.187 | 4 | 5 |
| 256 | 2 | 44 | 28 | -16 | 4589 | 4313 | -276 | +0.015 | +0.015 | 6 | 5 |
| 512 | 2 | 52 | 36 | -16 | 5411 | 4770 | -641 | +0.015 | +0.015 | 9 | 6 |
| 1024 | 2 | 60 | 36 | -24 | 6793 | 6434 | -359 | +0.015 | +0.015 | 12 | 6 |
| 2048 | 2 | 68 | 44 | -24 | 9126 | 7787 | -1339 | +0.015 | +0.015 | 17 | 12 |
| 4096 | 2 | 76 | 44 | -32 | 13081 | 11373 | -1708 | -0.095 | -0.095 | 30 | 22 |
| 8192 | 2 | 84 | 52 | -32 | 21051 | 16666 | -4385 | -0.144 | -0.144 | 52 | 47 |


## R=4 (SSR) comparison: r2 vs r22

| N | R | R2 DSP | R22 DSP | ΔDSP | R2 LUTs | R22 LUTs | ΔLUT | R2 WNS | R22 WNS | R2 BRAM | R22 BRAM |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 4 | 44 | 44 | +0 | 6357 | 4973 | -1384 | +0.013 | +0.013 | 0 | 4 |
| 256 | 4 | 76 | 60 | -16 | 8179 | 7042 | -1137 | +0.013 | +0.013 | 8 | 10 |
| 1024 | 4 | 108 | 76 | -32 | 11201 | 9897 | -1304 | +0.013 | +0.013 | 18 | 12 |
| 4096 | 4 | 140 | 92 | -48 | 18897 | 16233 | -2664 | +0.013 | +0.013 | 36 | 26 |
| 8192 | 4 | 156 | 92 | -64 | 27387 | 23966 | -3421 | -0.095 | -0.095 | 59 | 47 |


## R=8 (SSR) comparison: r2 vs r22

| N | R | R2 DSP | R22 DSP | ΔDSP | R2 LUTs | R22 LUTs | ΔLUT | R2 WNS | R22 WNS | R2 BRAM | R22 BRAM |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 8 | 76 | 76 | +0 | 12192 | 10374 | -1818 | -0.165 | -0.165 | 0 | 4 |
| 256 | 8 | 140 | 108 | -32 | 16178 | 14773 | -1405 | -0.165 | -0.165 | 0 | 8 |
| 1024 | 8 | 204 | 140 | -64 | 20343 | 19230 | -1113 | -0.165 | -0.165 | 24 | 20 |
| 4096 | 8 | 268 | 172 | -96 | 29734 | 28342 | -1392 | -0.165 | -0.165 | 48 | 24 |
| 8192 | 8 | 300 | 204 | -96 | 39506 | 34188 | -5318 | -0.165 | -0.165 | 72 | 52 |


*ΔDSP negative = r22 saves DSPs (target ~-50%); ΔLUT shows fabric cost. R>1 r22 uses M=N/R-point r22 lanes + same crossbar.*


## R=2 corner order (`r22b`): native -> bitreversed, P8

Same lanes, same crossbar, one RTL generic different: `REORDER_OUT=0` leaves
the DIF lane outputs bit-reversed (which *is* the `bitrev_N` emission at R=2,
because `bitrev_2` is the identity) and `fft_cross` addresses its WN row by
`bitrev(counter)`. Nothing else changes -- so DSPs are identical at every
size, and the savings are the per-lane reorder buffers simply not existing.

| N | r22 LUTs | r22b LUTs | dLUT | r22 LUTRAM | r22b LUTRAM | dLUTRAM | r22 DSP | r22b DSP | r22 BRAM | r22b BRAM | dBRAM | r22 WNS | r22b WNS |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 3207 | 3099 | -108 | 1053 | 973 | -80 | 20 | 20 | 2 | 2 | +0 | +0.015 | +0.015 |
| 128 | 3372 | 3338 | -34 | 794 | 786 | -8 | 28 | 28 | 5 | 3 | -2 | +0.187 | +0.015 |
| 256 | 4316 | 4263 | -53 | 1351 | 1335 | -16 | 28 | 28 | 5 | 3 | -2 | +0.015 | +0.015 |
| 512 | 4773 | 4698 | -75 | 1334 | 1302 | -32 | 36 | 36 | 6 | 4 | -2 | +0.015 | +0.015 |
| 1024 | 6437 | 6284 | -153 | 2425 | 2359 | -66 | 36 | 36 | 6 | 4 | -2 | +0.015 | +0.015 |
| 2048 | 7790 | 7585 | -205 | 3072 | 2944 | -128 | 44 | 44 | 12 | 8 | -4 | +0.015 | +0.015 |
| 4096 | 11376 | 11349 | -27 | 5643 | 5643 | +0 | 44 | 44 | 22 | 13 | -9 | -0.095 | -0.095 |
| 8192 | 16671 | 16627 | -44 | 9506 | 9506 | +0 | 52 | 52 | 47 | 30 | -17 | -0.144 | -0.098 |

- **DSPs identical at every N**, as predicted -- the reordering is free in
  multipliers.
- **BRAM/LUTRAM drop at every size** (up to -17 BRAM36 at N=8192, -4 at the
  customer's N=2048): the lane reorder ping-pongs are gone.
- **WNS is the same (+0.015) at every size except N=128 and N=8192.** At
  N=8192 the corner order is *better* (-0.098 vs -0.144, 12 vs 30 failing
  endpoints) because it removes exactly the lane-reorder-BRAM clock-to-out
  hop that became the limiter there. At N=128 it is worse (+0.015 vs +0.187),
  and the reason is measured, not assumed: the critical path there is
  `u_cross/x_*_reg -> u_cross/dout_*_reg` -- the final rescale/saturate stage
  in pure fabric, no DSP-internal hop, no BRAM hop. With the reorder buffers
  gone the tool packs the pipeline differently and this stage (which the
  native core hides behind a longer path elsewhere) becomes binding. It is
  the same +0.015 floor that R=2 measures at almost every other size, so the
  corner order did not introduce a new limit -- it stopped being outrun.
  Both meet 450 MHz.

## R=2 corner-order IFFT (`r22i`): bitreversed -> native, P8

The transpose of the forward corner (doc/plan_p8_ssr_orders.md 3b): the
R-point inverse runs FIRST (add/sub + `W_N^{-p}`, one quantize to
sample_width), then each lane's arrival is reordered bitrev_M -> native and
fed to the EXISTING M-point DIF-IDFT lane (`fft_top_r22`, INVERSE=1,
REORDER_OUT=1). The wrapper is `rtl/fft_ssr_r22_inv.v` -- a 5-stage pipeline
(input -> add/sub+twiddle -> partial products at the DSP MREG -> fabric
combine -> round+sat). Cost over the forward corner: the two per-lane input
reorders (~4 BRAM36 at N=2048) and the wrapper's 4 DSPs (one complex
multiply); both lanes keep their own output reorder.

| N | r22b LUTs | r22i LUTs | dLUT | r22i LUTRAM | r22i DSP | r22i BRAM | r22b WNS | r22i WNS | r22i FEP |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 3099 | 3398 | +299 | 1143 | 20 | 2 | +0.015 | -0.020 | 104 |
| 128 | 3338 | 3486 | +148 | 813 | 28 | 7 | +0.015 | -0.020 | 104 |
| 256 | 4263 | 4524 | +261 | 1375 | 28 | 7 | +0.015 | -0.252 | 111 |
| 512 | 4698 | 4993 | +295 | 1375 | 36 | 8 | +0.015 | -0.252 | 111 |
| 1024 | 6284 | 6762 | +478 | 2495 | 36 | 8 | +0.015 | -0.252 | 111 |
| 2048 | 7585 | 8188 | +603 | 3207 | 44 | 16 | +0.015 | -0.527 | 141 |
| 4096 | 11349 | 11738 | +389 | 5647 | 44 | 31 | -0.095 | -0.527 | 159 |
| 8192 | 16627 | 17108 | +481 | 9511 | 52 | 64 | -0.144 | -0.527 | 188 |

- **DSPs = forward corner + 4** at every size (the wrapper's complex
  multiply; the lanes' 40 are identical).
- **The +LUTs and +BRAM are the two per-lane input reorders** (and the
  wrapper's round/sat fabric) -- the forward corner deliberately has no lane
  reorders at all, so the inverse's extra memory is the price of the
  transpose route. It is exactly the ~4 BRAM36 the plan quoted for N=2048
  (the r22i row shows +8 over r22b because the lane output reorders -- which
  r22b removes -- are present here too).
- **WNS: -0.020 (N=64/128) -> -0.252 -> -0.527 (N>=2048)**, all on the
  LANE's own delay-line BRAM -> combine path (`dline_*_bram -> c3_*`), the
  same path the forward r22/r22b cores carry at +0.015. The extra reorders
  and wrapper displace the placement, so the lane's path stops being hidden
  behind a longer one. The shipping bar is the routed result: N=2048
  post-route WNS **-0.67 (141 FEP)** with BOTH the default and the
  aggressive directive recipe (`impl_aggr_inv.tcl`) -- the lane dline->c3
  path is placement-bound (the input reorders push the dline BRAMs apart),
  not strategy-bound. At 450 MHz (2.222 ns) the worst row (N>=2048, -0.527
  post-synth) is +0.195 and MEETS; the forward corner's +0.015 @ 500 MHz is
  not reached. Open item: the r22 DIT lane (removes the input reorders
  entirely) or an r22 memory-policy pass on the dline.
- Values are **bit-exact vs the golden model** (tolerance 0 -- the inverse
  has ONE quantization point, the wrapper's a1 quantize, mirrored exactly by
  the RTL); `compare.py` in the export checks that, plus positional markers.
  The exported N=2048 tree self-verifies PASS bit-exact (15314 samples).
- The FFT(corner) -> IFFT(corner) round trip recovers the input at
  2^-log2(N) -- the TX/RX fast-convolution pair (tests/test_rtl_ssr_orders.py).


## Radix-2^3 SDF (`r23`): S7

The S7 radix-2³ core (`rtl/fft_sdf_r23.v` + `rtl/fft_stage_r23.v`):
3-sample-per-pass triples (one 8-point kernel stage does the work of
three r2 stages) + auto-derived r22 leftover pairs. NTRIP = the largest
t in 1..3 with `3t <= NSTAGES`, `(NSTAGES-3t)` even and the smallest
triple `G = N>>(3t) >= 8`; `NPAIRL = (NSTAGES-3t)/2`. DSP count =
`4 x (NTRIP + NPAIRL)` (one shared complex multiplier per triple and
per pair); R=2 adds 4 DSPs for the crossbar (2 lanes x M-point engines
+ `fft_cross`, the P8 corner order native -> bitreversed -- the r23
lanes emit bit-reversed natively, so NO lane reorder buffers).

Swept on **xcku5p-ffva676-2-e** (speed grade -2; the URAM288 min-period
limit makes URAM unusable at 500 MHz on -1, see doc/uram_study.md),
OOC synthesis @ 500 MHz, 16-bit samples / 18-bit twiddles, 1 shift per
stage:

### R=1

| N | split (T+P) | DSP | LUTs | FFs | LUTRAM | BRAM36 | WNS(ns) | FEP |
|---:|:---:|---:|---:|---:|---:|---:|---:|---:|
| 512 | 1+3 | 16 | 3513 | 4460 | 932 | 7 | +0.073 | 0 |
| 1024 | 2+2 | 16 | 5598 | 6321 | 1602 | 14.5 | +0.066 | 0 |
| 2048 | 1+4 | 20 | 3870 | 4573 | 782 | 20 | +0.073 | 0 |
| 4096 | 2+3 | 20 | 5252 | 6432 | 1206 | 30 | +0.073 | 0 |
| 8192 | 3+2 | 20 | 7553 | 8305 | 2100 | 45.5 | +0.066 | 0 |
| 16384 | 2+4 | 24 | 6351 | 6574 | 1728 | 82 | +0.073 | 0 |
| 32768 | 3+3 | 24 | 8629 | 8556 | 2994 | 153 | +0.028 | 0 |

**Every R=1 row MEETS 500 MHz** (0 failing endpoints). Functionally
bit-exact vs the golden chain in both directions at every row.

### R=2 (SSR, 2 samples/clock)

| N | lane M | DSP | LUTs | FFs | LUTRAM | BRAM36 | WNS(ns) | FEP |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 256 | 128 | 28 | 5653 | 7982 | 1059 | 14 | +0.066 | 0 |
| 1024 | 512 | 36 | 7461 | 9308 | 1871 | 14 | +0.073 | 0 |
| 2048 | 1024 | 36 | 11754 | 13052 | 3180 | 29 | +0.066 | 0 |
| 4096 | 2048 | 44 | 8585 | 9575 | 1477 | 40 | +0.073 | 0 |
| 8192 | 4096 | 44 | 11961 | 13325 | 2198 | 60 | +0.073 | 0 |
| 16384 | 8192 | 44 | 17390 | 17126 | 3731 | 91 | -0.087 | 12 |
| 32768 | 16384 | 52 | 17151 | 13815 | 2404 | 164 | -0.517 | 58 |
| 65536 | 32768 | 52 | 28196 | 17485 | 3987 | 306 | -0.240 | 52 |

**R=2 meets 500 MHz through N=8192**; N=16384 misses by 0.087 ns (12
endpoints), N=32768 by 0.517. Functionally bit-exact (within the SSR
R/2+1-LSB tolerance, measured worst delta 1 LSB) vs
SSRGoldenModel(arch="r23") at every verified size: N=1024..32768 fwd+inv,
65536 fwd (`spikes/S7_r23/rtl_bringup/bringup_ssr23.py`). N=512 R=2 is
unsupported (lane M=256 = the small-G limitation, same as R=1 N=256).

Notes:
- DSP = 4*(NTRIP+NPAIRL) per lane + 4 for the crossbar; the corner
  order saves the reorder buffers entirely (vs the r22 native->native
  SSR, which carries 2M x 2W ping-pong BRAMs per lane).
- BRAM36 grows steeply at the largest N (306 at 65536 R=2, 153 at
  32768 R=1) -- the triple-chain LUTRAM rings spill into block RAM
  under the default policy; the URAM knob (`USE_URAM`, see
  doc/uram_study.md) moves the big rings to URAM288 at SG2 (~59 BRAM36
  freed at N=32768 R=1) and is the follow-up for these rows.
- N=256 R=1 is unsupported (no valid triple count; the small-G r23
  variant is the documented extension).

Repro: `FFT_PART=xcku5p-ffva676-2-e python3 -m src.datasheet_sweep --arch r23 --r1 256 512 1024 2048 4096 8192 16384 32768 65536 --jobs-dir build/datasheet_r23 -j 3`
(per-config caches in `build/datasheet_r23/N*_R{1,2}_r23/result.json`).

### Reading the table

- **Arch** -- `r2` = plain radix-2 SDF (`log2(N)` stages); `r22` = radix-2² SDF (one multiply per stage pair, P7 production core; re-pins the golden rounding, few-LSB SQNR-equal); `r23` = radix-2³ SDF (S7, 3 samples per pass through an 8-point kernel stage + r22 leftovers, R=1).
- **LUTs / FFs** -- CLB LUTs / CLB registers (post-synth, before opt_design trimming).
- **LUTRAM** -- LUTs carrying distributed RAM (delay lines, product FIFOs, crossbar WN table).
- **DSP** -- `r2` R=1: `4 x (num_stages - 2)` for N >= 8 (P6). `r22` R=1: ~`4 x ceil(stages/2)` with the odd-stage leftover as 0 DSP (shared multiply per pair). SSR: `R` lanes x `4 x (stages(N/R) - 2)` plus crossbar.
- **BRAM36** -- Block RAM tiles (36 Kb units). Halves (e.g. 10.5) indicate a RAMB18E2 counted as 0.5 tile; Vivado reports them as such.
- **clk/frame** -- clocks per output frame at steady state (= N/R; one sample-group per clock while ce is high).

### Timing notes

| architecture | WNS behavior | limiter |
|---|---|---|
| `r2` R=1 | +0.107 .. +0.113, flat across N (all MET) | product-FIFO LUTRAM read (pointer -> memory -> out) |
| `r22` R=1 | +0.048 .. +0.187, all MET (P7 step 7) | plain fabric: L0 input capture -> CARRY8 diff butterfly -> LUTRAM delay-line write; no DSP-internal path left. N=2048 post-route: -0.117 / 62 FEP with the default strategy, **MET (+0.003 / 0 FEP) with `place Explore` + `phys_opt/route AggressiveExplore` double pass** (4969 LUT, 20 DSP, 7.5 BRAM; recipe `spikes/S5_r22/dsp_probe/impl_aggr.tcl`). The thin margin is the v1 all-LUTRAM ring policy -- the placer already moves part of the deep rings into BRAM on its own, so an explicit `r22` memory policy + a re-derived registered-read stage is the real next lever |
| R=2 (`r2`, `r22`) | +0.015 MET through **N=2048**; -0.095 / -0.144 at N=4096 / 8192 | through P7 step 8 every R=2 row was capped at -0.020 by the crossbar's intra-DSP hop (`u_cross/g_pre pp*_reg`, MREG bypassed, N-independent, identical for both arches). Step 8 fixed that hop (early-hop im/re stagger + C-port re-alignment), so the rows through N=2048 now MEET 500 MHz and the largest N expose the *next* limiter: lane-reorder RAMB36 clock-to-out -> crossbar DSP input register (same path class as PLAN P5a). Verified as a trade, not a measurement artifact: re-synthesizing N=8192 R=2 `r22` with the pre-fix `fft_cross.v` gives -0.020 (intra-DSP), with the fixed one -0.144 (BRAM->DSP) |
| R=4 (`r2`, `r22`) | +0.013 MET through **N=4096**; -0.095 at N=8192 | same, one size later (M = N/4 lanes) |
| R=8 (`r2`, `r22`) | -0.165 flat | step 8 did NOT move R=8: its `g_pre` DSP hop is fixed but a different limiter takes over -- the sqrt(2)/2 scalar-multiply lo/hi split (`qq_* -> fhi_*`, 8 levels, 6 CARRY8 of pure fabric). Separate work item |
| R=2 `r22b` (P8 corner, native→bitrev) | +0.015 MET through **N=2048**; -0.095 / -0.098 at N=4096 / 8192 | the corner order deletes the lane reorder buffers, so the N=8192 row improves (-0.098 vs -0.144, 12 vs 30 FEP); at N=128 the binding path is instead the final rescale/saturate stage (`u_cross/x_*_reg -> dout_*_reg`, pure fabric, see the r22b section below). Same DSPs as `r22` at every N |

**Achievable clock with the step-8 crossbar -- the shipping figure.** Reading the same
synth numbers at a 450 MHz period (2.222 ns) instead of 500 MHz: **every R=2/R=4/R=8
config in the table MEETS it**, worst case R=8 -0.165 -> **+0.057**, R=2 N=8192
-0.144 -> **+0.078** (r22b: -0.098 -> **+0.124**), R=4 N=8192 -0.095 -> **+0.127**, and the
customer-relevant N=2048 R=2 (native or corner) sits at **+0.237**. 450 MHz is therefore
the honest safe-closure claim for SSR at 500-MHz-class silicon; 500 MHz is met by R=1
at every N and by R=2/R=4 up to N=2048/N=4096. Restoring 500 MHz at the largest SSR
sizes means breaking the lane-reorder BRAM clock-to-out hop (a `ram_schedule`/memory-policy
pass on the lane reorder buffers, or an extra fabric register that the tool will not
absorb into the DSP) and the R=8 split-product path -- deliberately not spent here.

The `r22` DSP savings grow with N (44→24 at N=8192, -45%): one general multiply per stage pair replaces two, with the odd-stage leftover (`N=64/128/...` odd `log2`) as fabric only. LUTs also drop for `r22` (16692→13238 at N=8192 R=1; -5337 at N=8192 R=8) because the pair shares control and the delay-line depth cascades 4x slower. With P7 step 7 the `r22` R=1 core meets the 500 MHz gate at every N in the same corner as `r2` (+0.187..+0.048 vs +0.107..+0.113), i.e. **~half the DSPs and ~20% fewer LUTs at no timing cost**; the remaining SSR gap is the shared crossbar, which is the same work item for both architectures.

### Memory policy

Delay lines / product FIFOs follow the decided cutoffs
(doc/mem_cutoffs.md): <=1024 bits distributed, <262144 bits block
(RAMB36E2), >=262144 bits URAM288. Twiddle ROMs go to block RAM at
N >= 256 (S4). SSR builds add per-lane reorder buffers (2M x width)
which move to block RAM from M >= 64 (visible as the BRAM36 jumps in
the R>=2 rows) -- except the P8 corner order (`r22b`), which does not
instantiate them at all (the DIF lanes emit bit-reversed p natively;
see the r22b section). The crossbar WN ROM is deliberately distributed
(async read for the pre-twiddle multiply); at large N x R it dominates
LUTRAM growth -- the one clear follow-up if larger R x N corners are
needed.

### Repro

```bash
python3 -m src.datasheet_sweep -j 4 --jobs-dir build/datasheet
# per-config caches in build/datasheet/N*_R*_{r2,r22,r22b}/result.json
# tables: build/datasheet/datasheet.md + comparison_r1.csv
# r22b = the P8 corner order (R=2 only): --arch all or --arch r22b
```

Tool flakiness seen while refreshing these tables: `vivado -mode batch`
segfaulted (rc 139, ~6 s in) once, on N=512 R=2 `r2`, and the identical
re-run succeeded. Worth a retry loop in the sweep before anyone chases such
a row as a design result.
