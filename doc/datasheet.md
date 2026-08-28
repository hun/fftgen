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
| 64 | 2 | r2 | 3553 | 5596 | 1435 | 28 | 0 | 0 | +0.015 | 0 | 32 |
| 64 | 2 | r22 | 3204 | 4010 | 1051 | 20 | 2 | 0 | +0.015 | 0 | 32 |
| 64 | 4 | r2 | 6357 | 8940 | 2288 | 44 | 0 | 0 | +0.013 | 0 | 16 |
| 64 | 4 | r22 | 4973 | 6445 | 978 | 44 | 4 | 0 | +0.013 | 0 | 16 |
| 64 | 8 | r2 | 12192 | 16041 | 3751 | 76 | 0 | 0 | -0.165 | 52 | 8 |
| 64 | 8 | r22 | 10374 | 12753 | 2845 | 76 | 4 | 0 | -0.165 | 52 | 8 |
| 128 | 1 | r2 | 2149 | 3535 | 906 | 20 | 2 | 0 | +0.107 | 0 | 128 |
| 128 | 1 | r22 | 2004 | 2362 | 660 | 12 | 1.5 | 0 | +0.187 | 0 | 128 |
| 128 | 2 | r2 | 3930 | 6509 | 1554 | 36 | 4 | 0 | +0.107 | 0 | 64 |
| 128 | 2 | r22 | 3369 | 4432 | 792 | 28 | 5 | 0 | +0.187 | 0 | 64 |
| 256 | 1 | r2 | 2529 | 4011 | 1128 | 24 | 3.5 | 0 | +0.107 | 0 | 256 |
| 256 | 1 | r22 | 2204 | 2624 | 648 | 16 | 2 | 0 | +0.187 | 0 | 256 |
| 256 | 2 | r2 | 4589 | 7524 | 1837 | 44 | 6 | 0 | +0.015 | 0 | 128 |
| 256 | 2 | r22 | 4313 | 5180 | 1349 | 28 | 5 | 0 | +0.015 | 0 | 128 |
| 256 | 4 | r2 | 8179 | 12907 | 3070 | 76 | 8 | 0 | +0.013 | 0 | 64 |
| 256 | 4 | r22 | 7042 | 8756 | 1548 | 60 | 10 | 0 | +0.013 | 0 | 64 |
| 256 | 8 | r2 | 16178 | 24189 | 5819 | 140 | 0 | 0 | -0.165 | 52 | 32 |
| 256 | 8 | r22 | 14773 | 17845 | 4285 | 108 | 8 | 0 | -0.165 | 52 | 32 |
| 512 | 1 | r2 | 3144 | 4501 | 1518 | 28 | 5 | 0 | +0.107 | 0 | 512 |
| 512 | 1 | r22 | 2962 | 3017 | 1184 | 16 | 2 | 0 | +0.187 | 0 | 512 |
| 512 | 2 | r2 | 5411 | 8483 | 2290 | 52 | 9 | 0 | +0.015 | 0 | 256 |
| 512 | 2 | r22 | 4770 | 5712 | 1332 | 36 | 6 | 0 | +0.015 | 0 | 256 |
| 1024 | 1 | r2 | 4201 | 5011 | 2246 | 32 | 7 | 0 | +0.113 | 0 | 1024 |
| 1024 | 1 | r22 | 3533 | 3240 | 1492 | 20 | 4.5 | 0 | +0.082 | 0 | 1024 |
| 1024 | 2 | r2 | 6793 | 9471 | 3087 | 60 | 12 | 0 | +0.015 | 0 | 512 |
| 1024 | 2 | r22 | 6434 | 6510 | 2423 | 36 | 6 | 0 | +0.015 | 0 | 512 |
| 1024 | 4 | r2 | 11201 | 16866 | 4502 | 108 | 18 | 0 | +0.013 | 0 | 256 |
| 1024 | 4 | r22 | 9897 | 11322 | 2588 | 76 | 12 | 0 | +0.013 | 0 | 256 |
| 1024 | 8 | r2 | 20343 | 31902 | 7345 | 204 | 24 | 0 | -0.165 | 80 | 128 |
| 1024 | 8 | r22 | 19230 | 22526 | 5399 | 140 | 20 | 0 | -0.165 | 80 | 128 |
| 2048 | 1 | r2 | 6120 | 5564 | 3644 | 36 | 10.5 | 0 | +0.113 | 0 | 2048 |
| 2048 | 1 | r22 | 5251 | 3677 | 2872 | 20 | 7.5 | 0 | +0.082 | 0 | 2048 |
| 2048 | 2 | r2 | 9126 | 10514 | 4574 | 68 | 17 | 0 | +0.015 | 0 | 1024 |
| 2048 | 2 | r22 | 7787 | 6973 | 3070 | 44 | 12 | 0 | +0.015 | 0 | 1024 |
| 4096 | 1 | r2 | 9830 | 6186 | 6386 | 40 | 17.5 | 0 | +0.113 | 0 | 4096 |
| 4096 | 1 | r22 | 7639 | 3963 | 4868 | 24 | 15 | 0 | +0.082 | 0 | 4096 |
| 4096 | 2 | r2 | 13081 | 11634 | 7179 | 76 | 30 | 0 | -0.095 | 11 | 2048 |
| 4096 | 2 | r22 | 11373 | 7884 | 5641 | 44 | 22 | 0 | -0.095 | 11 | 2048 |
| 4096 | 4 | r2 | 18897 | 20932 | 8922 | 140 | 36 | 0 | +0.013 | 0 | 1024 |
| 4096 | 4 | r22 | 16233 | 13884 | 5918 | 92 | 26 | 0 | +0.013 | 0 | 1024 |
| 4096 | 8 | r2 | 29734 | 39698 | 12123 | 268 | 48 | 0 | -0.165 | 52 | 512 |
| 4096 | 8 | r22 | 28342 | 27902 | 9473 | 172 | 24 | 0 | -0.165 | 52 | 512 |
| 8192 | 1 | r2 | 16692 | 6729 | 11816 | 44 | 32.5 | 0 | +0.113 | 0 | 8192 |
| 8192 | 1 | r22 | 13238 | 4540 | 9624 | 24 | 31.5 | 0 | +0.048 | 0 | 8192 |
| 8192 | 2 | r2 | 21051 | 12959 | 12536 | 84 | 52 | 0 | -0.144 | 30 | 4096 |
| 8192 | 2 | r22 | 16666 | 8493 | 9504 | 52 | 47 | 0 | -0.144 | 30 | 4096 |
| 8192 | 4 | r2 | 27387 | 23166 | 14195 | 156 | 59 | 0 | -0.095 | 11 | 2048 |
| 8192 | 4 | r22 | 23966 | 15657 | 11123 | 92 | 47 | 0 | -0.095 | 11 | 2048 |
| 8192 | 8 | r2 | 39506 | 43849 | 17774 | 300 | 72 | 0 | -0.165 | 52 | 1024 |
| 8192 | 8 | r22 | 34188 | 29711 | 11770 | 204 | 52 | 0 | -0.165 | 52 | 1024 |

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


### Reading the table

- **Arch** -- `r2` = plain radix-2 SDF (`log2(N)` stages); `r22` = radix-2² SDF (one multiply per stage pair, P7 production core; re-pins the golden rounding, few-LSB SQNR-equal).
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

**Achievable clock with the step-8 crossbar -- the shipping figure.** Reading the same
synth numbers at a 450 MHz period (2.222 ns) instead of 500 MHz: **every R=2/R=4/R=8
config in the table MEETS it**, worst case R=8 -0.165 -> **+0.057**, R=2 N=8192
-0.144 -> **+0.078**, R=4 N=8192 -0.095 -> **+0.127**, and the customer-relevant
N=2048 R=2 sits at **+0.237**. 450 MHz is therefore the honest safe-closure claim for
SSR at 500-MHz-class silicon; 500 MHz is met by R=1 at every N and by R=2/R=4 up to
N=2048/N=4096. Restoring 500 MHz at the largest SSR sizes means breaking the
lane-reorder BRAM clock-to-out hop (a `ram_schedule`/memory-policy pass on the lane
reorder buffers, or an extra fabric register that the tool will not absorb into the
DSP) and the R=8 split-product path -- deliberately not spent here.

The `r22` DSP savings grow with N (44→24 at N=8192, -45%): one general multiply per stage pair replaces two, with the odd-stage leftover (`N=64/128/...` odd `log2`) as fabric only. LUTs also drop for `r22` (16692→13238 at N=8192 R=1; -5337 at N=8192 R=8) because the pair shares control and the delay-line depth cascades 4x slower. With P7 step 7 the `r22` R=1 core meets the 500 MHz gate at every N in the same corner as `r2` (+0.187..+0.048 vs +0.107..+0.113), i.e. **~half the DSPs and ~20% fewer LUTs at no timing cost**; the remaining SSR gap is the shared crossbar, which is the same work item for both architectures.

### Memory policy

Delay lines / product FIFOs follow the decided cutoffs
(doc/mem_cutoffs.md): <=1024 bits distributed, <262144 bits block
(RAMB36E2), >=262144 bits URAM288. Twiddle ROMs go to block RAM at
N >= 256 (S4). SSR builds add per-lane reorder buffers (2M x width)
which move to block RAM from M >= 64 (visible as the BRAM36 jumps in
the R>=2 rows). The crossbar WN ROM is deliberately distributed
(async read for the pre-twiddle multiply); at large N x R it dominates
LUTRAM growth -- the one clear follow-up if larger R x N corners are
needed.

### Repro

```bash
python3 -m src.datasheet_sweep -j 4 --jobs-dir build/datasheet
# per-config caches in build/datasheet/N*_R*_{r2,r22}/result.json
# tables: build/datasheet/datasheet.md + comparison_r1.csv
```

Tool flakiness seen while refreshing these tables: `vivado -mode batch`
segfaulted (rc 139, ~6 s in) once, on N=512 R=2 `r2`, and the identical
re-run succeeded. Worth a retry loop in the sweep before anyone chases such
a row as a design result.
