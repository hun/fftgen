# Spike S5 — radix-2^2 folding: numerical contract (P7)

`contract_check.py` settles the load-bearing questions before any RTL.

## Findings

### 1. Rotation identity holds bit-exactly in the canonical table

    T[i + N/4] == -j * T[i]   (forward)     T[i + N/4] == +j * T[i]   (inverse)

verified exhaustively over N = 8..1024, widths (18,12,8,10), decimal
variants, both directions. This is a consequence of the magnitude-first
construction (twiddles.py): A/B are |cos|/|sin| magnitudes of the SAME
angle, so the quarter-wave mirror is an exact sign flip -- no rounding
in the rotation. **The R2^2 +/-j diff combine is therefore free and
exact; it never changes values.**

### 2. The R2^2 contract is NOT bit-identical to the pinned radix-2 golden

The classic R2^2 (radix-4 DIF) per 4-sample group computes THREE
products with ONE rounding each:

    y2 = round(cmul((a0+a2)-(a1+a3), T[2i]),        td + s1)        (s1 = s_{2m+1})
    y1 = round(cmul((a0-a2) - j(a1-a3), T[i]),      td + s0 + s1)   (fused shift)
    y3 = round(cmul((a0-a2) + j(a1-a3), T[3i]),     td + s0 + s1)

while the pinned radix-2 sequence computes FOUR products with FOUR
roundings (c0 = cmul(d0,T[i]) and c1 = cmul(d1,T[i+N/4]) rounded
separately at td+s0, then re-multiplied/combined at stage 2m+1).
Since round_shift() is not rotation-invariant (round-half-up bias), the
two formulations differ at rounding boundaries:

    plain vs R2^2:  max|delta| = 1-2 LSB at 16-bit/18-bit twiddles,
    a few LSB (<= ~10) for wide samples / narrow twiddles -- the bound
    scales with the fractional headroom (compound double-vs-single
    rounding + downstream noise accumulation), and does NOT grow with N.
    SQNR:           identical to 2 decimals (both match the float ref)

**PLAN appendix A's "same recursion + same per-stage shift points =>
bit-identical" claim is FALSE for the fused Q-format datapath.** The
merge moves the product rounding points (the diff combine happens before
the multiply). The difference is small (a few LSB, width-dependent) and numerically benign,
but the golden must be RE-PINNED to the R2^2 contract.

### 3. The DSP saving is real

One general complex multiply per stage pair (3 products per 4-group at
~75% duty) instead of two stages' worth (4 products at 50% duty each):

    R=1 N=2048:  P6 36 DSPs  ->  R2^2 ~20   (pairs (0,1)..(8,9) x 4)
    R=2 N=2048:  P6 68 DSPs  ->  R2^2 ~36   (2 x 1024-pt engines, n=10:
                 pairs (0,1)..(6,7) x 4 each = 16/engine + 4 crossbar)
    (Spiral reference: 40 at R=2 N=2048)

Odd stage counts leave the last stage as a plain radix-2 stage (W^0
only -> P6 trivial_prod, 0 DSPs). For even n the last pair's groups
collapse to a single i=0 group (all W^0) -> free.

## Re-pinned R2^2 contract (candidate new golden)

`fft_fixed_batch_r22()` in contract_check.py is the reference: DIF,
stage pairs (2m, 2m+1) merged; per group (b, j) with block size
N/4^m, group depth N/4^{m+1}, twiddle stride 4^m:

    b0 = round(a0+a2, s_{2m});  b1 = round(a1+a3, s_{2m})
    d0 = a0-a2;  d1 = a1-a3                          (exact)
    y0 = round(b0+b1, s_{2m+1})
    y2 = round(cmul(b0-b1, T[2j*4^m]),  td + s_{2m+1})
    y1 = round(cmul(d0 -/+ j*d1, T[j*4^m]),    td + s_{2m}+s_{2m+1})
    y3 = round(cmul(d0 +/- j*d1, T[3j*4^m]),   td + s_{2m}+s_{2m+1})

Sum paths keep the two sub-stage roundings (as plain); the merged
product paths use ONE fused rounding at td+s_{2m}+s_{2m+1}. Verified:
SQNR == plain == float, max|delta| vs plain == 1-2 LSB (16-bit
inputs; a few LSB for wide samples / narrow twiddles), N = 8..1024,
fwd+inv.

## Decision

P7 proceeds with a **re-pinned golden** (R2^2 mode in golden.py /
golden_ssr.py), NOT the bit-identical reuse the old appendix claimed.
The value delta (a few LSB, width-dependent) is within the existing
quantization contract's noise floor; SQNR is unchanged. RTL must reproduce fft_fixed_batch_r22
bit-exactly.

## Remaining P7 work (after this spike)

1. Promote fft_fixed_batch_r22 into src/golden.py (R2^2 mode, DIF+DIT,
   batch + streaming _SDFStageR22), pin via unit tests.
2. Derive the R2^2 SDF schedule (phase structure, delay lines: the R2^2
   stage needs TWO sub-delay lines of depths D and 2D; FSM/preloads).
3. New RTL stage module (fft_stage_r22.v): 4-sample group butterfly,
   ONE complex multiply (4 DSPs) time-multiplexed over the group,
   +/-j combine in fabric, P6 trivial_prod for the free sub-paths.
4. Twiddle ROM re-layout (pair bases, stride 4^m slices).
5. Bit-exact verification vs the re-pinned golden + timing sweep.

## Synthesis: DSP reduction proven, timing needs pipelining

`synth_check.py` synthesizes the full R2² core on KU5P:

| config | R2² DSPs | vs P6 | vs original |
|---|---|---|---|
| N=2048 R=1 | **20** | 36 | 44 |

Exactly the projection (5 general pairs x 4 DSPs + trivial leftover).

Timing: NOT yet closed at 500 MHz (WNS −5.232 @ 2 ns; critical path
~7.2 ns regardless of clock). The R2² stage's datapath is fully
combinational (product -> F4 combine -> fused round_shift); the plain
core's 500 MHz closure came from the 10-layer pipeline with DSP
register absorption (AREG/BREG/MREG/PREG). The R2² stage needs the
same treatment (P7 milestone 8): register the multiply operands into
DSP input regs, the products into MREG, split the F4 combine into
fabric stages, and the round_shift into a staging layer -- mirroring
the model's combinational step with the RTL pipeline registers.

## Timing closure (WIP) -- the pipelining plan

The critical path (~7.2 ns) is the LUTRAM async-read mux (rp -> RAMS64E1
-> MUXF7/8/9) PLUS the DSP48E2s used combinationally (the RTL's `wire
prod = m*w` has no registers, so AREG/BREG/MREG/PREG are not absorbed).

The pipeline (piped_model.py, WIP -- NOT yet bit-exact):
  L0 capture   registered reads + input + twiddle + phase (k_r)
  L1 butterfly s0/d0, s1/d1, sd = s0(ram-read) - s1, the +/-j combines,
               y0 = s0(ram-read) + s1   (the s0 must come from the SRAM
               read capture, NOT a held register)
  L2 products  the 4 real multiplies (DSP MREG)
  L3 combine   re = rr - ii, im = ri + ir
  L4 shift     round-half-up staging (products AND y0, both at depth 4
               so the output mux aligns)
  L5 write     pfifo + sram/dram/dline/ram + output

KEY discipline (the bug source): each write/mux uses its register at its
OWN pipeline depth, gated by the matching delayed phase:
  ram write   depth 0 (raw input)  gate k
  sram/dram   depth 1 (L1 regs)    gate k1 (= k delayed 1)
  dline       depth 1              gate k1
  pfifo       depth 4 (shift_p)    gate k4
  output mux  depth 4 (y0_r)       gate k4; pfifo lag = D + 1

Status: the values are NOT yet bit-exact (the first-stage y0 is off);
this is a multi-iteration effort like the plain core's NLAYERS 7->10.
The synthesis proof stands: 20 DSPs at N=2048 R=1; 500 MHz closure is
the remaining P7 work.

## Pipelined RTL (WIP, committed) -- status after the model milestone

The L0-L5 pipelined `rtl/fft_stage_r22.v` is WRITTEN but NOT yet bit-exact
(Verilator: N=4 stage outputs (10275, 23021) at cycle 7 instead of
(5138, -21258); the y0_raw chain's values are subtly wrong -- the
y0_raw3 after cycle 5 shows ~2x the expected y0_raw of the a3, so a
duplicated add or a wrong sram value is lurking).

Verified structure (the model is the contract -- piped_model.py):
- the L1 is COMBINATIONAL from the async reads/input (NOT registered
  captures) -- a registered L1 is one clock behind the model's step
- the lag-D lines (sram/dram/dline) MUST be written at the ARRIVAL
  clock (depth 0, the current combinational s_x/d_x, plain sp): a
  nonblocking write cannot be seen in the same clock, so the model's
  depth-1 write + (sp-1) address is NOT synthesizable as-is
- the pfifo write and the output mux use the depth-4 registers (k4)
- the shift select uses k3 (the product's capture phase)

Next session: VCD-trace the y0_raw/s0/sx values against the model's
per-cycle values (spikes/S5_r22/piped_model.py prints the model's
cycle-by-cycle outputs); the y0_raw chain, the sram write value, and
the round_shift staging are the suspects.

## L0-register retiming (user directive: ALL ram reads need a register)

The timing-critical async LUTRAM reads must land in registers before
they fan out. The piped DIF model is being retimed with an explicit
L0 register stage (the reads captured at the posedge, the L1 consumes
them one step later). This shifts the whole schedule: the a2/a3
butterflies arrive one clock later, the s0/s1 combine becomes
s_r + s0_r at step 3D+g+2, the products move to k3/k4/k5/k6/k7 gates,
the pfifo writes use shift_p2 at the k6<3D gate, the mux gate is k7.

STATUS: N=4 (D=1) is BIT-EXACT both directions. D>=2 fail at the
stagger: the y1/y3 twiddle must be selected by the DATA's group
(the sp-based group, = (sp-1) mod D in this schedule), not the read
phase -- the retimed schedule decouples the two (the d0/d1 of group g
are read at sp=g+1, and the twiddle T[g*4^m] must follow the group,
not the k phase). This is the next fix: capture the twiddle (and the
group) with the L0 d_r/dl_r read instead of the k phase.

## L0 retiming progress -- D>=2 pfifo gate alignment (the blocker)

N=4 (D=1) is BIT-EXACT. The D>=2 staggers fail at the pfifo writes:
the products are computed at the right steps (the y2 of group 0 ready
as shift_p2 after s12 for N=8), but the pfifo gate `k6 < 3D` has the
[3D,4D) phases in the OFF region, so the D=2 y2s (ready at k6=6,7)
are blocked and their shift_p2 values get overwritten by the next
products. For D=1 the ready steps happened to land on k6 in [0,3D)
(that is why N=4 passes); for D>=2 they land on [3D,4D).

Next: the pfifo write gate must cover the phase where EACH product is
ready (the write should follow the shift_p2 pipeline depth, not a
fixed phase window). Likely fix: gate the write by "a product from a
block's group is ready" i.e. the k7 window shifted, or track the ready
steps directly. The twiddle-by-group fix (g_r2 = (sp-1)%D) is correct
(N=4's 8/8 and N=8's 12/16 confirm the y0/y1/y3 paths).

Reference for later: thoughts/cmult.v -- a 3-DSP fully pipelined
complex multiplier (Vivado docs, Gauss trick) vs our 4-DSP Karatsuba.
Could cut the R2^2 stage's DSP count if the ALU/pre-adder structure
accepts it (the F4 combine needs the cross terms though).
