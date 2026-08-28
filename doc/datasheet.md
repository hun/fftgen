# fftgen datasheet

## Resource & timing -- KU5P OOC synthesis @ 500 MHz

xcku5p-ffva676-1-e, Vivado 2026.1 out-of-context synthesis,
`create_clock 2.0 ns`, 16-bit samples / 18-bit twiddles (Q17), auto
scaling schedule, PIPE_DEPTH=10 (golden NLAYERS), post-warm preload
packs applied (`FFTGEN_PRELOADS`) for `r2`. `r22` uses the production
P7 core (`rtl/fft_sdf_r22.v` + `rtl/fft_stage_r22.v`, `K_PRELOAD` phase
alignment; `rtl/fft_ssr_r22.v` for R>1) -- the same files the export
flow ships. Post-synth estimates; the `r2` R=1 and `r22` R=1 paths close
post-route (see PLAN.md P5a / S3 findings and the P7 step 7 note on
`rtl/fft_stage_r22.v`).

Twiddle ROM style is `auto`: block RAM from N >= 256
(doc/mem_cutoffs.md S4).

Sweep regenerated 2026-08-28 with the P6 trivial-twiddle reduction
(`r2`: `4 x (stages - 2)` DSPs/engine) and the P7 `r22` core (one
multiply per stage pair, production core). The `r22` rows changed from
the 2026-08-27 sweep because of the P7 step 7 DSP-pipeline fix (staggered
C-port pairing + natural-width operands): `r22` R=1 went from
-0.020 / 288-576 FEP to MET at every N. Regenerate with:

    python3 -m src.datasheet_sweep -j 4            # both arches, all N×R
    python3 -m src.datasheet_sweep --arch r2 -j 4  # r2 only (legacy)

Legacy spike sweep remains at `spikes/S2_timing/datasheet_sweep.py`.

| N | R | Arch | LUTs | FFs | LUTRAM | DSP | BRAM36 | URAM | WNS(ns) | FEP | clk/frame |
|---:|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1 | r2 | 1848 | 3031 | 766 | 16 | 1 | 0 | +0.107 | 0 | 64 |
| 64 | 1 | r22 | 1564 | 1991 | 384 | 12 | 1.5 | 0 | +0.187 | 0 | 64 |
| 128 | 1 | r2 | 2149 | 3535 | 906 | 20 | 2 | 0 | +0.107 | 0 | 128 |
| 128 | 1 | r22 | 2004 | 2362 | 660 | 12 | 1.5 | 0 | +0.187 | 0 | 128 |
| 256 | 1 | r2 | 2529 | 4011 | 1128 | 24 | 3.5 | 0 | +0.107 | 0 | 256 |
| 256 | 1 | r22 | 2204 | 2624 | 648 | 16 | 2 | 0 | +0.187 | 0 | 256 |
| 512 | 1 | r2 | 3144 | 4501 | 1518 | 28 | 5 | 0 | +0.107 | 0 | 512 |
| 512 | 1 | r22 | 2962 | 3017 | 1184 | 16 | 2 | 0 | +0.187 | 0 | 512 |
| 1024 | 1 | r2 | 4201 | 5011 | 2246 | 32 | 7 | 0 | +0.113 | 0 | 1024 |
| 1024 | 1 | r22 | 3533 | 3240 | 1492 | 20 | 4.5 | 0 | +0.082 | 0 | 1024 |
| 2048 | 1 | r2 | 6120 | 5564 | 3644 | 36 | 10.5 | 0 | +0.113 | 0 | 2048 |
| 2048 | 1 | r22 | 5251 | 3677 | 2872 | 20 | 7.5 | 0 | +0.082 | 0 | 2048 |
| 4096 | 1 | r2 | 9830 | 6186 | 6386 | 40 | 17.5 | 0 | +0.113 | 0 | 4096 |
| 4096 | 1 | r22 | 7639 | 3963 | 4868 | 24 | 15 | 0 | +0.082 | 0 | 4096 |
| 8192 | 1 | r2 | 16692 | 6729 | 11816 | 44 | 32.5 | 0 | +0.113 | 0 | 8192 |
| 8192 | 1 | r22 | 13238 | 4540 | 9624 | 24 | 31.5 | 0 | +0.048 | 0 | 8192 |
| 64 | 2 | r2 | 3537 | 5660 | 1419 | 28 | 0 | 0 | -0.020 | 96 | 32 |
| 64 | 2 | r22 | 3188 | 4074 | 1035 | 20 | 2 | 0 | -0.020 | 96 | 32 |
| 128 | 2 | r2 | 3914 | 6541 | 1538 | 36 | 4 | 0 | -0.020 | 96 | 64 |
| 128 | 2 | r22 | 3353 | 4464 | 776 | 28 | 5 | 0 | -0.020 | 96 | 64 |
| 256 | 2 | r2 | 4574 | 7556 | 1821 | 44 | 6 | 0 | -0.020 | 96 | 128 |
| 256 | 2 | r22 | 4298 | 5212 | 1333 | 28 | 5 | 0 | -0.020 | 96 | 128 |
| 512 | 2 | r2 | 5394 | 8515 | 2274 | 52 | 9 | 0 | -0.020 | 96 | 256 |
| 512 | 2 | r22 | 4753 | 5744 | 1316 | 36 | 6 | 0 | -0.020 | 96 | 256 |
| 1024 | 2 | r2 | 6775 | 9502 | 3071 | 60 | 12 | 0 | -0.020 | 96 | 512 |
| 1024 | 2 | r22 | 6412 | 6536 | 2407 | 36 | 6 | 0 | -0.020 | 96 | 512 |
| 2048 | 2 | r2 | 9107 | 10543 | 4558 | 68 | 17 | 0 | -0.020 | 96 | 1024 |
| 2048 | 2 | r22 | 7772 | 7006 | 3054 | 44 | 12 | 0 | -0.020 | 96 | 1024 |
| 4096 | 2 | r2 | 13058 | 11662 | 7163 | 76 | 30 | 0 | -0.020 | 96 | 2048 |
| 4096 | 2 | r22 | 11353 | 7914 | 5625 | 44 | 22 | 0 | -0.020 | 96 | 2048 |
| 8192 | 2 | r2 | 21039 | 12992 | 12520 | 84 | 52 | 0 | -0.020 | 96 | 4096 |
| 8192 | 2 | r22 | 16654 | 8526 | 9488 | 52 | 47 | 0 | -0.020 | 96 | 4096 |
| 64 | 4 | r2 | 6336 | 9036 | 2272 | 44 | 0 | 0 | -0.020 | 288 | 16 |
| 64 | 4 | r22 | 4952 | 6541 | 962 | 44 | 4 | 0 | -0.020 | 288 | 16 |
| 256 | 4 | r2 | 8161 | 12939 | 3054 | 76 | 8 | 0 | -0.020 | 288 | 64 |
| 256 | 4 | r22 | 7024 | 8788 | 1532 | 60 | 10 | 0 | -0.020 | 288 | 64 |
| 1024 | 4 | r2 | 11179 | 16898 | 4486 | 108 | 18 | 0 | -0.020 | 288 | 256 |
| 1024 | 4 | r22 | 9876 | 11354 | 2572 | 76 | 12 | 0 | -0.020 | 288 | 256 |
| 4096 | 4 | r2 | 18876 | 20944 | 8906 | 140 | 36 | 0 | -0.020 | 288 | 1024 |
| 4096 | 4 | r22 | 16215 | 13897 | 5902 | 92 | 26 | 0 | -0.020 | 288 | 1024 |
| 8192 | 4 | r2 | 27365 | 23227 | 14179 | 156 | 59 | 0 | -0.020 | 288 | 2048 |
| 8192 | 4 | r22 | 23942 | 15720 | 11107 | 92 | 47 | 0 | -0.020 | 288 | 2048 |
| 64 | 8 | r2 | 12138 | 16202 | 3735 | 76 | 0 | 0 | -0.165 | 724 | 8 |
| 64 | 8 | r22 | 10320 | 12914 | 2829 | 76 | 4 | 0 | -0.165 | 724 | 8 |
| 256 | 8 | r2 | 16154 | 24343 | 5803 | 140 | 0 | 0 | -0.165 | 724 | 32 |
| 256 | 8 | r22 | 14749 | 17999 | 4269 | 108 | 8 | 0 | -0.165 | 724 | 32 |
| 1024 | 8 | r2 | 20326 | 31934 | 7329 | 204 | 24 | 0 | -0.165 | 724 | 128 |
| 1024 | 8 | r22 | 19214 | 22558 | 5383 | 140 | 20 | 0 | -0.165 | 724 | 128 |
| 4096 | 8 | r2 | 29727 | 39735 | 12107 | 268 | 48 | 0 | -0.165 | 724 | 512 |
| 4096 | 8 | r22 | 28316 | 27935 | 9457 | 172 | 24 | 0 | -0.165 | 724 | 512 |
| 8192 | 8 | r2 | 39501 | 43885 | 17758 | 300 | 72 | 0 | -0.165 | 724 | 1024 |
| 8192 | 8 | r22 | 34164 | 29748 | 11754 | 204 | 52 | 0 | -0.165 | 724 | 1024 |

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
| 64 | 2 | 28 | 20 | -8 | 3537 | 3188 | -349 | -0.020 | -0.020 | 0 | 2 |
| 128 | 2 | 36 | 28 | -8 | 3914 | 3353 | -561 | -0.020 | -0.020 | 4 | 5 |
| 256 | 2 | 44 | 28 | -16 | 4574 | 4298 | -276 | -0.020 | -0.020 | 6 | 5 |
| 512 | 2 | 52 | 36 | -16 | 5394 | 4753 | -641 | -0.020 | -0.020 | 9 | 6 |
| 1024 | 2 | 60 | 36 | -24 | 6775 | 6412 | -363 | -0.020 | -0.020 | 12 | 6 |
| 2048 | 2 | 68 | 44 | -24 | 9107 | 7772 | -1335 | -0.020 | -0.020 | 17 | 12 |
| 4096 | 2 | 76 | 44 | -32 | 13058 | 11353 | -1705 | -0.020 | -0.020 | 30 | 22 |
| 8192 | 2 | 84 | 52 | -32 | 21039 | 16654 | -4385 | -0.020 | -0.020 | 52 | 47 |


## R=4 (SSR) comparison: r2 vs r22

| N | R | R2 DSP | R22 DSP | ΔDSP | R2 LUTs | R22 LUTs | ΔLUT | R2 WNS | R22 WNS | R2 BRAM | R22 BRAM |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 4 | 44 | 44 | +0 | 6336 | 4952 | -1384 | -0.020 | -0.020 | 0 | 4 |
| 256 | 4 | 76 | 60 | -16 | 8161 | 7024 | -1137 | -0.020 | -0.020 | 8 | 10 |
| 1024 | 4 | 108 | 76 | -32 | 11179 | 9876 | -1303 | -0.020 | -0.020 | 18 | 12 |
| 4096 | 4 | 140 | 92 | -48 | 18876 | 16215 | -2661 | -0.020 | -0.020 | 36 | 26 |
| 8192 | 4 | 156 | 92 | -64 | 27365 | 23942 | -3423 | -0.020 | -0.020 | 59 | 47 |


## R=8 (SSR) comparison: r2 vs r22

| N | R | R2 DSP | R22 DSP | ΔDSP | R2 LUTs | R22 LUTs | ΔLUT | R2 WNS | R22 WNS | R2 BRAM | R22 BRAM |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 8 | 76 | 76 | +0 | 12138 | 10320 | -1818 | -0.165 | -0.165 | 0 | 4 |
| 256 | 8 | 140 | 108 | -32 | 16154 | 14749 | -1405 | -0.165 | -0.165 | 0 | 8 |
| 1024 | 8 | 204 | 140 | -64 | 20326 | 19214 | -1112 | -0.165 | -0.165 | 24 | 20 |
| 4096 | 8 | 268 | 172 | -96 | 29727 | 28316 | -1411 | -0.165 | -0.165 | 48 | 24 |
| 8192 | 8 | 300 | 204 | -96 | 39501 | 34164 | -5337 | -0.165 | -0.165 | 72 | 52 |


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
| `r22` R=1 | +0.048 .. +0.187, all MET (P7 step 7) | plain fabric: L0 input capture -> CARRY8 diff butterfly -> LUTRAM delay-line write; the DSP is off the limiting path. N=2048 post-route -0.117 / 62 FEP on that same fabric family (post-synth +0.082) -- the deep `r22` v1 LUTRAM rings are the next lever (BRAM/URAM policy + a re-derived registered-read stage) |
| R=2 / R=4 (`r2`, `r22`) | -0.020 flat, N-independent | NOT the lanes: the crossbar's intra-DSP hop (`u_cross/g_pre pp*_reg`: A/B-reg -> preadd -> mult -> ALU -> PREG, MREG bypassed) -- identical for both arches, so the `r22` lane savings come for free. Measured -0.021 post-route with TNS -0.152 at R=2 N=8192 (`r2`, AggressiveExplore double pass) -- skew-dominated synth estimate |
| R=8 (`r2`, `r22`) | -0.165 flat | same crossbar hop plus the sqrt(2)/2 scalar-multiply split path; not yet post-routed |

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
