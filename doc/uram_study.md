# UltraRAM (URAM288) utilization study

Question: the target FPGA has a lot of free URAM but little BRAM -- where
can the FFT cores use URAM288 instead of RAMB36E2?

All measurements: Vivado 2026.1 OOC `synth_design`, 16-bit samples /
18-bit twiddles, 1 shift/stage, `create_clock 2.0 ns` (500 MHz), on
`xcku5p-ffva676-{1,2}-e` (both speed grades measured). r23 core at
N=32768 -- the BRAM-heaviest configuration in the datasheet
(152.5 BRAM36, the largest rows being the triple-0 rings + twiddle ROM).

---

## 1. Memory inventory (what BRAM is spent on)

### r23 core (rtl/fft_stage_r23.v, per triple, G = N >> (3m+3))

| array | depth x width (re+im) | bits | default style |
|---|---|---:|---|
| ring0 (raw inputs) | 4G x 16 x2 | 128G | block |
| ringA_s | 2G x 18 x2 | 72G | block |
| ringA_d0/d1, ringB_p/q/q1, rbbm, rbbp, rr1, rr3 | 9 x (G x 18 x2) | 648G | block |
| pf1..pf7 (product FIFOs) | 7 x (G x 16 x2) | 224G | auto -> BRAM at large G |
| tw_rom (twiddles) | 8G x 36 | 288G | block |
| pf*, small leftovers | G x 16..18 | -- | LUTRAM |

At N=32768 (G0/G1/G2 = 4096/512/64) the triple-0 arrays alone are
~2.9 Mb + a 1.15 Mb twiddle ROM -- i.e. > 75% of the core's 152.5
BRAM36 sits in the first triple.

### r22 core / r2 core / SSR

- r22 stage rings are <= 2D = N/2 deep x 16: at N=8192 the first pair's
  rings (4096x16, 2048x17) are the BRAM hogs (~12 of 31.5 BRAM36), the
  rest shrink x4 per pair.
- r2 core (`fft_sdf.v`): stage lines up to N/2 deep -- same shape
  (32.5 BRAM36 at N=8192).
- SSR builds: per-lane reorder ping-pong 2M x W (M = N/R lanes) grows
  linearly with N/R; the crossbar WN ROM is deliberately LUTRAM.
  These are the URAM candidates for R>1 at large N (not yet wired).

## 2. What URAM288 can and cannot do here

From doc/mem_cutoffs.md S1 (KU3P, Vivado 2026.1) plus this study:

- `ram_style = "ultra"` maps our codings onto URAM288, INCLUDING the
  read-old/write-new same-address and paired-line shapes (S1 findings
  4-5: "same-address never becomes URAM" was a hint artifact). The
  URAM288 double-pumped ports deliver read-first semantics, matching
  the golden contract; single clock only -- both hold for this design.
- **Read-latency contract is unchanged**: every block-style memory in
  the cores already uses the 1-cycle registered-read (DOUT) pattern,
  which is exactly URAM's behavioral read latency. No schedule change.
- **URAM288 CANNOT be initialized** (no INIT support; contents are
  only implicitly zero at power-up). Consequences:
  - twiddle ROMs (`$readmemh`) must stay in BRAM/LUTRAM -- always;
  - the data rings are fine: they are write-before-read in steady
    state and the golden model zero-fills them (matching URAM's
    implicit zeros); the sim-only `initial` blocks remain for X
    cleanliness in RTL simulation.
- **URAM288 clock period is slower than BRAM**: measured KU5P-1
  min period 2.088 ns -> **cannot run at 500 MHz at all** (PW
  violation on every URAM pin, 64 endpoints). At SG2 the min period
  fits 2 ns (PW check +0.300) -- URAM is a 500-MHz-capable resource
  only on speed grade -2 (or the ~455 MHz corner on -1).
- URAM clock-to-out is worse than BRAM (~0.5-1 ns more), and cascaded
  URAM (depth > 4096) adds further delay. Arrays must be <= 72 bit
  wide; depth comes in 4096 chunks (one 4096x18 ring = 1 URAM at 25%
  width utilization -- wasteful but the point is freeing BRAM).

## 3. Implementation (USE_URAM generic)

`rtl/fft_stage_r23.v` gained `parameter integer USE_URAM = 0`:

- `RING_STYLE` (11 G-deep rings + ringA_s): "block" -> "ultra" when 1
- `PF_STYLE` (pf1..pf7 product FIFOs): "auto" -> "ultra" when 1
- `RING0_STYLE` (the 4G-deep raw-input ring): stays "block" -- see 4
- `TW_STYLE` (twiddle ROM): stays "block" always -- URAM cannot be
  initialized (section 2)
- `rtl/fft_sdf_r23.v` passes `USE_URAM` to the three triple stages
  (default 0 = the shipped datasheet configuration).

RTL simulation is attribute-blind; bit-exactness was re-verified after
the edit (N=8192 + N=32768 bringup, 100.00%).

## 4. Measurements (r23, N=32768)

| configuration | BRAM36 | URAM | WNS | FEP | note |
|---|---:|---:|---:|---:|---|
| SG1, all BRAM (datasheet) | 152.5 | 0 | -0.195 | 150 | post-synth |
| SG1, rings+pf in URAM | 80 | 64 | -1.554 | 300 | all top paths: tw ROM URAM CK->Q |
| SG2, all BRAM | 152.5 | 0 | **+0.028** | 0 | post-synth |
| SG2, rings+pf+ROM in URAM | 84 | 64 | -0.501 | 55 | 16/20 top paths: ring0 URAM cascade CK->Q |
| SG2, rings+pf URAM, ring0+ROM BRAM | 90 | 64 | -0.154 | 40 | 14/20 top: fabric g_r->ROM path; 6: ringA_s URAM |
| SG2, +ringA_s BRAM too | 93.5 | 64 | -0.154 | 25 | binding path = the fabric g_r->ROM arc (baseline-critical), NOT a URAM arc |

Findings:

1. **The mapping works**: 152.5 BRAM36 -> ~84 BRAM + 64 URAM (the
   twiddle ROMs account for ~37.5 of the remaining BRAM; ring0 ~16).
2. **SG1 cannot use URAM at 500 MHz** (2.088 ns min period). SG2 can.
3. **The twiddle ROM must stay in BRAM** (no URAM init), and at SG1 its
   URAM clock-to-out would anyway break the DOUT -> DSP-BREG contract
   (WNS -1.554 when it was ultra-hinted).
4. **ring0 (the only 4G-deep array) fails as URAM** (4-high cascade
   CK->Q -> the a0_r read register -> butterfly path, -0.501 at SG2);
   the 4096-deep single-URAM rings show no failing paths in the top-20.
5. With ring0 back in BRAM the gap shrinks to **-0.154 / 40 FEP**; with
   ringA_s also in BRAM: **93.5 BRAM + 64 URAM at -0.154 / 25 FEP**, and
   the worst path is the fabric g_r -> twiddle-ROM address arc -- the
   SAME path that is the all-BRAM baseline's worst (+0.028). The residual
   delta is URAM-routing congestion around triple-0, not a URAM read arc;
   the r22 N=2048 precedent (-0.117 post-synth -> +0.003 post-route with
   the aggressive-explore recipe) makes post-route closure plausible.

## 4b. The small-N boundary: N=2048 R=2 (SSR) is NOT worth it

Measured (xcku5p-ffva676-2-e, fft_ssr_r23 N=2048 SSR=2, USE_URAM=1):

| build | BRAM36 | URAM | WNS | FEP |
|---|---:|---:|---:|---:|
| baseline (USE_URAM=0) | 29 | 0 | +0.066 | 0 |
| rings+pf in URAM | 25 | **64** | -0.010 | 11 |

At this size every ring is shallow (G=128: 128-512 deep x 18b), so each
array occupies a whole URAM288 at >90% emptiness -- 64 URAMs (16 Mb) to
free 4 BRAM tiles, plus a small timing regression (the URAM CK->Q arcs
replacing half-tile BRAM reads). The freed tiles are also only 4 because
most of the 29 BRAM are per-array RAMB18 half-tiles that Vivado packs
efficiently at these shapes.

**Rule of thumb (break-even)**: USE_URAM pays when the biggest G-deep
rings are >= ~4096 deep, i.e. N >= 16384 R=1 / N >= 32768 R=2 (the
datasheet rows where BRAM36 hits 90-306). Below that, keep USE_URAM=0.

## 5. Recommendation

- **Shippable knob**: `USE_URAM=1` on the r23 core at **speed grade -2**
  (or any ~500 MHz-capable URAM part): swaps **~59 BRAM36 for 64 URAM288**
  at N=32768 (152.5 -> 93.5 BRAM, the twiddle ROMs + ring0 + ringA_s
  stay BRAM) at a -0.154 post-synth WNS whose binding path is the
  baseline-critical fabric arc -- post-route effort expected to close
  500 MHz (verify before shipping), else ~490 MHz.
- SG1 parts: URAM cannot run 500 MHz (2.088 ns PW limit) -- URAM use
  there means a <= 455 MHz clock.
- SG1 parts: URAM is not usable at 500 MHz in this design (hard PW
  limit); only relevant for <= 450 MHz clocks.
- The same treatment applies to the r2/r22 cores' deep first-stage
  rings and the SSR reorder buffers (~12 BRAM36 at r22 N=8192; the
  reorder buffers grow linearly with N/R) -- not yet wired, same
  1-cycle read contract, same caveats.
- ROMs are forever BRAM/LUTRAM.
