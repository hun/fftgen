# fftgen datasheet

## Resource & timing -- KU5P OOC synthesis @ 500 MHz

xcku5p-ffva676-1-e, Vivado 2026.1 out-of-context synthesis,
`create_clock 2.0 ns`, 16-bit samples / 18-bit twiddles (Q17), auto
scaling schedule, PIPE_DEPTH=10 (golden NLAYERS), post-warm preload
packs applied (`FFTGEN_PRELOADS`) for `r2`. `r22` uses the P7 spike
top (`rtl/fft_stage_r22.v`) with `K_PRELOAD` phase alignment. Post-synth
estimates -- the binding paths close post-route (see PLAN.md P5a / S3
findings).

Twiddle ROM style is `auto`: block RAM from N >= 256
(doc/mem_cutoffs.md S4).

Sweep generated 2026-08-27 with the P6 trivial-twiddle reduction
(`r2`: `4 x (stages - 2)` DSPs/engine) and the P7 `r22` core (one
multiply per stage pair, spike top). Regenerate with:

    python3 -m src.datasheet_sweep -j 4            # both arches, all N×R
    python3 -m src.datasheet_sweep --arch r2 -j 4  # r2 only (legacy)

Legacy spike sweep remains at `spikes/S2_timing/datasheet_sweep.py`.

| N | R | Arch | LUTs | FFs | LUTRAM | DSP | BRAM36 | URAM | WNS(ns) | FEP | clk/frame |
|---:|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1 | r2 | 1848 | 3031 | 766 | 16 | 1 | 0 | +0.107 | 0 | 64 |
| 64 | 1 | r22 | 1538 | 2049 | 378 | 12 | 1.5 | 0 | -0.020 | 288 | 64 |
| 128 | 1 | r2 | 2149 | 3535 | 906 | 20 | 2 | 0 | +0.107 | 0 | 128 |
| 128 | 1 | r22 | 1974 | 2421 | 648 | 12 | 1.5 | 0 | -0.020 | 288 | 128 |
| 256 | 1 | r2 | 2529 | 4011 | 1128 | 24 | 3.5 | 0 | +0.107 | 0 | 256 |
| 256 | 1 | r22 | 2160 | 2715 | 628 | 16 | 2 | 0 | -0.020 | 384 | 256 |
| 512 | 1 | r2 | 3144 | 4501 | 1518 | 28 | 5 | 0 | +0.107 | 0 | 512 |
| 512 | 1 | r22 | 2908 | 3115 | 1148 | 16 | 2 | 0 | -0.020 | 384 | 512 |
| 1024 | 1 | r2 | 4201 | 5011 | 2246 | 32 | 7 | 0 | +0.113 | 0 | 1024 |
| 1024 | 1 | r22 | 3438 | 3363 | 1424 | 20 | 4.5 | 0 | -0.020 | 480 | 1024 |
| 2048 | 1 | r2 | 6120 | 5564 | 3644 | 36 | 10.5 | 0 | +0.113 | 0 | 2048 |
| 2048 | 1 | r22 | 5118 | 3808 | 2740 | 20 | 7.5 | 0 | -0.020 | 480 | 2048 |
| 4096 | 1 | r2 | 9830 | 6186 | 6386 | 40 | 17.5 | 0 | +0.113 | 0 | 4096 |
| 4096 | 1 | r22 | 7371 | 4111 | 4608 | 24 | 15 | 0 | -0.020 | 576 | 4096 |
| 8192 | 1 | r2 | 16692 | 6729 | 11816 | 44 | 32.5 | 0 | +0.113 | 0 | 8192 |
| 8192 | 1 | r22 | 12706 | 4696 | 9108 | 24 | 31.5 | 0 | -0.020 | 576 | 8192 |
| 64 | 2 | r2 | 3537 | 5660 | 1419 | 28 | 0 | 0 | -0.020 | 96 | 32 |
| 128 | 2 | r2 | 3914 | 6541 | 1538 | 36 | 4 | 0 | -0.020 | 96 | 64 |
| 256 | 2 | r2 | 4574 | 7556 | 1821 | 44 | 6 | 0 | -0.020 | 96 | 128 |
| 512 | 2 | r2 | 5394 | 8515 | 2274 | 52 | 9 | 0 | -0.020 | 96 | 256 |
| 1024 | 2 | r2 | 6775 | 9502 | 3071 | 60 | 12 | 0 | -0.020 | 96 | 512 |
| 2048 | 2 | r2 | 9107 | 10543 | 4558 | 68 | 17 | 0 | -0.020 | 96 | 1024 |
| 4096 | 2 | r2 | 13058 | 11662 | 7163 | 76 | 30 | 0 | -0.020 | 96 | 2048 |
| 8192 | 2 | r2 | 21039 | 12992 | 12520 | 84 | 52 | 0 | -0.020 | 96 | 4096 |
| 64 | 4 | r2 | 6336 | 9036 | 2272 | 44 | 0 | 0 | -0.020 | 288 | 16 |
| 256 | 4 | r2 | 8161 | 12939 | 3054 | 76 | 8 | 0 | -0.020 | 288 | 64 |
| 1024 | 4 | r2 | 11179 | 16898 | 4486 | 108 | 18 | 0 | -0.020 | 288 | 256 |
| 4096 | 4 | r2 | 18876 | 20944 | 8906 | 140 | 36 | 0 | -0.020 | 288 | 1024 |
| 64 | 8 | r2 | 12138 | 16202 | 3735 | 76 | 0 | 0 | -0.165 | 724 | 8 |
| 256 | 8 | r2 | 16154 | 24343 | 5803 | 140 | 0 | 0 | -0.165 | 724 | 32 |
| 1024 | 8 | r2 | 20326 | 31934 | 7329 | 204 | 24 | 0 | -0.165 | 724 | 128 |

### R=1 comparison: r2 vs r22 (DSP savings)

| N | R | R2 DSP | R22 DSP | ΔDSP | R2 LUTs | R22 LUTs | ΔLUT | R2 WNS | R22 WNS | R2 BRAM | R22 BRAM |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1 | 16 | 12 | -4 | 1848 | 1538 | -310 | +0.107 | -0.020 | 1 | 1.5 |
| 128 | 1 | 20 | 12 | -8 | 2149 | 1974 | -175 | +0.107 | -0.020 | 2 | 1.5 |
| 256 | 1 | 24 | 16 | -8 | 2529 | 2160 | -369 | +0.107 | -0.020 | 3.5 | 2 |
| 512 | 1 | 28 | 16 | -12 | 3144 | 2908 | -236 | +0.107 | -0.020 | 5 | 2 |
| 1024 | 1 | 32 | 20 | -12 | 4201 | 3438 | -763 | +0.113 | -0.020 | 7 | 4.5 |
| 2048 | 1 | 36 | 20 | -16 | 6120 | 5118 | -1002 | +0.113 | -0.020 | 10.5 | 7.5 |
| 4096 | 1 | 40 | 24 | -16 | 9830 | 7371 | -2459 | +0.113 | -0.020 | 17.5 | 15 |
| 8192 | 1 | 44 | 24 | -20 | 16692 | 12706 | -3986 | +0.113 | -0.020 | 32.5 | 31.5 |

*ΔDSP negative = r22 saves DSPs (target ~-50%); ΔLUT negative = r22 also saves LUTs at small N, larger savings at 4K/8K. WNS -0.020 for r22 is the intra-stage pipe (288–576 FEP); P7 has not yet closed timing — see below.*

### Reading the table

- **Arch** -- `r2` = plain radix-2 SDF (`log2(N)` stages); `r22` = radix-2² SDF (one multiply per stage pair, P7 spike top, R=1 only; re-pins the golden rounding, few-LSB SQNR-equal).
- **LUTs / FFs** -- CLB LUTs / CLB registers (post-synth, before opt_design trimming).
- **LUTRAM** -- LUTs carrying distributed RAM (delay lines, product FIFOs, crossbar WN table).
- **DSP** -- `r2` R=1: `4 x (num_stages - 2)` for N >= 8 (P6). `r22` R=1: ~`4 x ceil(stages/2)` with the odd-stage leftover as 0 DSP (shared multiply per pair). SSR: `R` lanes x `4 x (stages(N/R) - 2)` plus crossbar.
- **BRAM36** -- Block RAM tiles (36 Kb units). Halves (e.g. 10.5) indicate a RAMB18E2 counted as 0.5 tile; Vivado reports them as such.
- **clk/frame** -- clocks per output frame at steady state (= N/R; one sample-group per clock while ce is high).

### Timing notes

| architecture | WNS behavior | limiter |
|---|---|---|
| `r2` R=1 | +0.107 .. +0.113, flat across N (all MET) | product-FIFO LUTRAM read (pointer -> memory -> out) |
| `r22` R=1 | -0.020 flat, 288–576 FEP | intra-stage L0→L1→L2 pipeline (the `r22` stage is deeper — `ram/sram/dram/dline` reads → `L0` capture → `L1` butterfly → `L2a/b` DSP chain); not yet timing-closed — P7 TODO (needs L0 register retiming + BRAM output reg) |
| R=2 / R=4 (`r2`) | -0.020 flat, N-independent | intra-DSP cascade hop (A/B-reg -> preadd -> mult -> ALU -> PREG); measured -0.021 post-route with TNS -0.152 at R=2 N=8192 (AggressiveExplore double pass) -- skew-dominated synth estimate |
| R=8 (`r2`) | -0.165 flat | same cascade hop plus the sqrt(2)/2 scalar-multiply split path; not yet post-routed |

The `r22` DSP savings grow with N (44→24 at N=8192, -45%): one general multiply per stage pair replaces two, with the odd-stage leftover (`N=64/128/...` odd `log2`) as fabric only. LUTs also drop for `r22` because the pair shares control/BRAM. Current `r22` WNS is not closed; the `r2` 500 MHz closure (+0.113) vs `r22` (-0.020) delta is the P7 timing work (register the four memory reads in L0, DSP AREG/BREG at L2a).

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
