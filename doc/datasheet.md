# fftgen datasheet

## Resource & timing -- KU5P OOC synthesis @ 500 MHz

xcku5p-ffva676-1-e, Vivado 2026.1 out-of-context synthesis,
`create_clock 2.0 ns`, 16-bit samples / 18-bit twiddles (Q17), auto
scaling schedule, PIPE_DEPTH=10 (golden NLAYERS), post-warm preload
packs applied (`FFTGEN_PRELOADS`). Post-synth estimates -- the binding
paths close post-route (see PLAN.md P5a / S3 findings).

Twiddle ROM style is `auto`: block RAM from N >= 256
(doc/mem_cutoffs.md S4).

Regenerate with:

    python3 spikes/S2_timing/datasheet_sweep.py -j 4

| N | R | LUTs | FFs | LUTRAM | DSP | BRAM36 | URAM | WNS(ns) | FEP | clk/frame |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1 | 1390 | 2739 | 696 | 24 | 1 | 0 | +0.700 | 0 | 64 |
| 128 | 1 | 1691 | 3243 | 836 | 28 | 2 | 0 | +0.700 | 0 | 128 |
| 256 | 1 | 2071 | 3719 | 1058 | 32 | - | 0 | +0.579 | 0 | 256 |
| 512 | 1 | 2686 | 4209 | 1448 | 36 | 5 | 0 | +0.485 | 0 | 512 |
| 1024 | 1 | 3755 | 4719 | 2176 | 40 | 7 | 0 | +0.471 | 0 | 1024 |
| 2048 | 1 | 5666 | 5272 | 3574 | 44 | - | 0 | +0.459 | 0 | 2048 |
| 4096 | 1 | 9377 | 5894 | 6316 | 48 | - | 0 | +0.287 | 0 | 4096 |
| 8192 | 1 | 16238 | 6437 | 11746 | 52 | - | 0 | +0.158 | 0 | 8192 |
| 64 | 2 | 2638 | 5076 | 1283 | 44 | 0 | 0 | -0.020 | 96 | 32 |
| 128 | 2 | 3017 | 5957 | 1398 | 52 | 4 | 0 | -0.020 | 96 | 64 |
| 256 | 2 | 3649 | 6972 | 1681 | 60 | 6 | 0 | -0.020 | 96 | 128 |
| 512 | 2 | 4478 | 7931 | 2134 | 68 | 9 | 0 | -0.020 | 96 | 256 |
| 1024 | 2 | 5834 | 8918 | 2931 | 76 | 12 | 0 | -0.020 | 96 | 512 |
| 2048 | 2 | 8191 | 9958 | 4418 | 84 | 17 | 0 | -0.020 | 96 | 1024 |
| 4096 | 2 | 12154 | 11082 | 7023 | 92 | 30 | 0 | -0.020 | 96 | 2048 |
| 8192 | 2 | 20085 | 12407 | 12380 | 100 | 52 | 0 | -0.020 | 96 | 4096 |
| 64 | 4 | 4527 | 7868 | 1992 | 76 | 0 | 0 | -0.020 | 288 | 16 |
| 256 | 4 | 6336 | 11771 | 2774 | 108 | 8 | 0 | -0.020 | 288 | 64 |
| 1024 | 4 | 9347 | 15730 | 4206 | 140 | 18 | 0 | -0.020 | 288 | 256 |
| 4096 | 4 | 17083 | 19785 | 8626 | 172 | 36 | 0 | -0.020 | 288 | 1024 |
| 64 | 8 | 8490 | 13866 | 3175 | 140 | 0 | 0 | -0.165 | 724 | 8 |
| 256 | 8 | 12506 | 22007 | 5243 | 204 | 0 | 0 | -0.165 | 724 | 32 |
| 1024 | 8 | 16662 | 29598 | 6769 | 268 | 24 | 0 | -0.165 | 724 | 128 |

> **P6 note (DSP column):** this sweep predates the trivial-twiddle-stage
> reduction; the measured DSP counts were 4 × stages per engine. Post-P6 the
> last two stages per engine are exact fabric products, so the DSP column
> is 8 lower per engine (4 lower per stage × 2 stages). Re-measured anchors:
> N=64 R=1 24→**16**, N=2048 R=1 44→**36** (WNS +0.113), N=2048 R=2 84→**68**
> (WNS −0.020, unchanged). LUTs/FFs rise slightly (fabric products replace
> DSPs); re-run `datasheet_sweep.py` for a full post-P6 table.


### Reading the table

- **LUTs / FFs** -- CLB LUTs / CLB registers (post-synth, before
  opt_design trimming).
- **LUTRAM** -- LUTs carrying distributed RAM (delay lines, product
  FIFOs, crossbar WN table).
- **DSP** -- R=1: 4 x (num_stages - 2) for N >= 8 (4-product complex
  multiply + C-port combine per general-twiddle stage; the last two
  stages' W^0/±j products are exact fabric logic, P6). Edge cases:
  N=2/4 -> 0 DSPs, N=8 -> 4 (stage 0 only). SSR: R lanes x 4 x
  (stages(N/R) - 2) plus the crossbar network (R-point lane DFT +
  pre-twiddle products).
- **clk/frame** -- clocks per output frame at steady state (= N/R;
  one sample-group per clock while ce is high).

### Timing notes

| architecture | WNS behavior | limiter |
|---|---|---|
| R=1 | +0.158 .. +0.700, degrades slowly with N | product-FIFO LUTRAM read (pointer -> memory -> out); all sizes MET |
| R=2 / R=4 | -0.020 flat, N-independent | intra-DSP cascade hop (A/B-reg -> preadd -> mult -> ALU -> PREG); measured -0.021 post-route with TNS -0.152 at R=2 N=8192 (AggressiveExplore double pass) -- skew-dominated synth estimate |
| R=8 | -0.165 flat | same cascade hop plus the sqrt(2)/2 scalar-multiply split path; not yet post-routed |

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
