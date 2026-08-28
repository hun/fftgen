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

## L0 retiming -- pfifo ring wrap (the LAST bug for D>=2)

N=4 is 8/8 bit-exact. N=8 raw is 11/12 -- only pos7 (the LAST product
of the block, the y3g1) reads the stale y3g0 from pfifo[2] instead of
the y3g1 at pfifo[0]. The pfifo ring (depth 2D=4) with the 3D=6
products/block and the lag D=2: the six reads cycle pr = 1,2,3,0,1,2
(the 6 product slots for 2 groups), but the LAST product's slot
(pfifo[0]) is written at s18 (the shift_p2 of the y3g1 is ready at
s17, written at s17 to pfifo[1] after the y1g1 slot) and read at
pr=0 (s21) -- one step/slot late. The fix needs the pfifo ring's
write/read pointer arithmetic to place the block's LAST product in the
slot the LAST read expects (the ring's wrap alignment at the block
boundary). The 4th new product (the block's 6th) should overwrite the
1st-read slot after its read.

Status: the L0 register retiming produces CORRECT product VALUES; the
pfifo delivery of the block's last product is one slot off for D>=2.

## The decisive finding: the pfifo write gate is D-dependent

The product-ready phases (the shift_p2 ready steps mapped to k6):
  D=1: y2@k6=0, y1@k6=1, y3@k6=2  -> gate k6 < 3D (N=4 8/8)
  D=2: 6 products @ k6 = 6,7,0,1,2,3 -> gate k6 in [0,2D) U [3D,4D)
The two gates conflict (D=1's y3 at k6=2 lands in [2D,3D) which D=2's
gate excludes). Root: the pipeline-delayed product-ready steps are not
a fixed phase window; they depend on where the combine lands vs the
block boundary (D=1's combine at 3D+2 -> products @ ~3D+6+1 -> phases
0..3D-1; D=2's at different relative steps). Fix: derive the READY
STEPS per D (the shift_p2 ready step for each of the 3D used products)
and gate the write on the ready steps, not a fixed k-window. This is
the last structural piece; the product VALUES are all correct.

## The pfifo ring-wrap root cause (precise, D=1 trace)

D=1 verified trace: the products y2/y1/y3 are in the shift_p2 after
s9/s10/s11. The writes fire at the gate steps: y2->pfifo[0] (s10,
k3 gate), y1->pfifo[1] (s12), y3->pfifo[1] (s13, overwriting the y1
after its read at s12). The reads: pr=0 at s11 (y2, out s12=pos1),
pr=1 at s12 (y1, out s13=pos2), pr=0 at s13 (read pfifo[0]=stale y2 --
the y3 at pfifo[1] missed; its read at pr=1 lands on s14 whose mux
selects the y0 (out s15=pos4)).

ROOT: the product WRITE steps are not one-per-block-period -- there is
one gap (s11 has no write) -- so the ring's write/read pointer parity
drifts for the LAST product. The write steps must be the 3D USED
products at the period-consecutive steps [W, W+1, ..., W+3D-1]. Since
the shift_p2 holds a new product EVERY step (the L2 makes one per
step), the write must fire on EVERY step whose shift_p2 is a USED
product -- step 11's product (a waste-c3) must be skipped AND the step
sequence compensated so the y3 lands at the pr=0 read. Fix options:
(a) delay the y3's pipeline by one more (shift_p3) so its write lands
at s14/pwfi[0]; (b) a one-shot address offset for the last product.

## k3 gate verified fully for D=1; D=2's group-1 y2 missing

The k3 gate (operand-phase [0,2D) U [3D,4D)) + shift_p2 writes is
verified for D=1: y2->pfifo[0] (s10) [y3 overwrites after the read],
y1->pfifo[1] (s11), y3->pfifo[0] (s12); the reads pr=0,1,0 at the
positions 1,2,3 deliver y2,y1,y3 at steps 12,13,14 (pos0=y0@11).
N=4 is 8/8 both directions.

N=8 (D=2): 12/16 -- the GROUP-1's y2 (pos4) is ZERO. The D=2's sd
products: the k3 mux selects sd at [6,8) (2 phases) but only ONE sd
product reaches the pfifo; the group-1's sd selection or its twiddle
(g_r2) pairing produces a missing/wrong write. The y2 product
ordering for the second group needs a check of the operand mux vs the
sd_r pipeline (the sd of group 1 at its combine step vs the k3 phase).

## The D=2 product window (decisive)

The D=2's shift_p2 holds the SIX products at CONSECUTIVE steps s12-s17
(y2g0, y3g0, y1g0, y2g1, y1g1, y3g1) with the pre-edge k3 phases
1,2,3,4,5,6. The fixed gate [0,2D) U [3D,4D) = [0,4) U [6,8) covers
1,2,3,6 but MISSES 4,5 (the y2g1 and y1g1) -- the group-1 products
are lost. D=1's three products at s9-s11 (k3 phases 3,0,1) happen to
fit [0,2) U [3,4). So the pfifo write must fire on the block's 3D
CONSECUTIVE product-ready steps (a product-window flag tracking the
first product's ready step), NOT a fixed phase window.

Next session: replace the phase-gated pfifo write with a per-block
product-window flag (set when the first product of the block is in the
shift_p2 pipeline, clear 3D steps later). This unifies D=1 and D=2.

## N=256+ chain alignment (the last piece)

N<=128 verified (12/12 for the 4..128 fwd+inv sweep). N=256+ single
frame fails at pos16. Each stage passes in isolation (D=64, D=16
both 512/512 fed raw samples; the D=16 also 512/512 fed the stage-0
stream at the aligned step). The 2-stage run-style chain diverges at
pos16 with the ROTATED values ((a,b)->(b,-a) -- a j-multiplied
neighbor), suggesting a k-phase or warmup-state misalignment in the
multi-stage feeding that does NOT appear in the single-stage or the
clean max(N) direct feed. The negative-step warmup (the chain feeds
steps -lat0..0) vs clean (0..) behavior is the suspect -- the phase-
gated writes during the negative warmup may differ. Next: trace the
2-stage chain's s1 input vs the clean feed at pos16 (the s1's k and
the sram/dram contents at the divergence).

## L0 retiming COMPLETE: 160/160 bit-exact (N=4..2048)

The final bug was the RING POINTERS: a chained stage is called with
pos-up (its own step index), so the rp/sp/pwp/pr_r must derive from
the step (pos+1 mod size), NOT the accumulated call count -- the
upstream warmup calls skewed the ring addresses by the upstream
latency mod D, misaligning the D-lag memories.

All paths verified: L0-registered reads, the s_r + s0_r combine at
3D+g+2, the per-D gates (mux k3, shift k5, pfifo k7 in [0,2D) U
[3D,4D)), the mux k7, the latency 3D+8. 160 configs (N 4..2048,
fwd+inv, sample widths 16/18/20/24, twiddle widths 16/18).

NEXT: mirror this retimed model in rtl/fft_stage_r22.v (register the
four reads, the k-chain to k7, the pfifo/window gates, the pos-derived
pointers -- the RTL equivalences are the pre-edge semantics), update
top_gen.py (the K_PRELOAD = -upstream latencies, the leftover preload
parity using the 3D+8 latencies), and re-verify + synthesize.

## L0-retimed RTL verified 16/16 (N=4..2048, fwd+inv)

rtl/fft_stage_r22.v rewritten as a 1:1 mirror of the verified
piped_model.py: L0 regs (the 4 async reads + input + g_r), the L1
wires from the L0 regs (y0_raw/sd pair s_r + the s0_r L1 reg), the
k-chain extended to k7, the gates (ram k, sram/dram/dline k1,
pfifo k7 in [0,2D) U [3D,4D), out k7, product mux + ROM select k3,
shift select k5), shift_p2, and the PHASE-DERIVED pointers (rp/pwp =
k mod 2D, sp = k mod D with D=1 special case, pr = (k-D) mod 2D) --
the chained stage's k is its own step index, so the ring addresses
follow the model's step-index pointers. The AREG/BREG regs are
dropped (the DSP absorbs the L1-reg mux); latency 3D+8. top_gen.py
updated (up_lat/K_PRELOAD/lat 3D+8; the leftover parity 3D+8 is odd
iff D odd, same as 3D+6).

rtl_check: 16/16 BIT-EXACT (skip = lat-1 consistently).
NEXT: KU5P synthesis at 500 MHz (WNS target >= 0, 20 DSPs).

## AREG/BREG DSP stage + shreg-extract: 16/16 bit-exact, WNS -0.02 syn / -0.123 impl

The user flagged the DSP registers (a, d, ad, b, m, p): the mapping
showed only MREG+PREG (the A/B inputs combinational). Re-added the
AREG/BREG operand registers (m_r/tr_r2 = the k3-mux and the BRAM
twiddle captured one clock before the multiply); the DSP report now
shows AREG=1 BREG=1 MREG=1 PREG=1. The multiply's combinational path
(B2->MULT->M_DATA->ALU, 1.85ns) is the new critical -- the DSP's
M-register pipeline must be forced (the final netlist bypasses the
MREG: the multiply+ALU run in one clock). Also forced shreg_extract
= no on the y0_raw/k chains (the SRL tap reads were on the critical
path). Latency 3D+9; verified 16/16 incl. N=2048 fwd+inv.

KU5P @ 500MHz: synth WNS -0.020 (DSP-internal), post-P&R WNS -0.123
with 160 endpoints: the write-data paths (x_r -> the rounded s_x/d_x
-> the RAMS32/D SP, ~2.09ns, route-dominated) + the BRAM-DOUT->c1
(1.86) + the tw_dout->DSP-B. Next: the 2-cycle BRAM read (the DOUT
CKO 1.4ns out of the L1 paths) and/or the post-route phys_opt.

## 2-cycle BRAM read exploration (WIP, NOT landed)

Goal: pull the BRAM DOUT CKO (~1.4ns) out of the L1 paths (the
post-P&R WNS -0.123 is dominated by the BRAM-DOUT->c1 (1.86) and the
x_r->write-data (2.09) paths). Attempted: a second read register
(L0b: a_q/s_q/d_q/dl_q + x_q) so the L1 adders see plain-FF inputs.

Findings:
- Fix 1: the input x MUST get the same +1 delay (x_q) -- the L1
  butterfly pairs the reads with the input, both must be the same
  clock's data. This was the first silent break (the all-zeros).
- Fix 2 (the model): the Python 2-cycle model must assign the r-regs
  from the OLD q-regs (reverse order: `a_r = a_q; ... a_q = cur_a`)
  to emulate the Verilog nonblocking -- a sequential `a_q = cur;
  a_r = a_q` gives NO delay.
- The minimal 2-cycle (the L0b+x_q with the ORIGINAL k-gates):
  values preserved (the stream is the old's shifted by one -- the
  first frame's head gets displaced), latency 3D+9; the emergence
  observed at lat-1 with a trailing zero -- the slice/emergence needs
  one more off-by-one pass.
- ANY gate-shift (mux k3->k4, shift k5->k7, pfifo k7->k9, writes
  k1->k2) BREAKS the pfifo values (2/8) -- the +1 does NOT propagate
  that way; the L0b's delay changes the select-vs-data relationship
  differently for the mux (data-relative) vs the pfifo (absolute
  window) gates. The correct mapping needs a careful model-first pass
  (the model = the contract) rather than the guess-and-check.
- The committed state is the a41f6bf + the duplicate-y0_raw5 removal
  + the ring zero-initialization (both verified-harmless): 20/20
  bit-exact N=4..2048 fwd+inv, synth WNS -0.020, post-P&R -0.123.

NEXT for the closure: finish the L0b mapping in the model (the
pfifo's slot semantics under the +1), then mirror + re-verify.

## Post-route phys_opt: WNS -0.051 (from -0.123), 36 eps

The default flow ran phys_opt before route only. Added a second
phys_opt + re-route after route_design: the KU5P impl WNS improved
from -0.123 to -0.051 (the top path: the BRAM DOUT -> the c3/sd L1
combines, dd ~1.79-1.85ns -- the residual is the RAMB DOUT CKO ~1.4
+ the L1 XOR/add logic). 20 DSPs, 36 failing endpoints. The 2-cycle
L0b read (a second read register) is the structural fix for the
DOUT-CKO; the model-first derivation of its exact gate mapping is
the next step (documented above).

## L0b (2-cycle read) -- conclusively NOT a simple +1

With the assignment order CORRECTED (the r-regs = the previous q-regs,
matching the Verilog nonblocking), the true 2-cycle model produces
values that match the RTL's garbage EXACTLY -- the +1 read-delay
BREAKS the schedule (the pfifo/products land in the wrong slots).
The earlier "32/32 bit-exact" was an artifact: the min2c's sequential
`a_q = cur; a_r = a_q` made the a_r = the CURRENT reads (a no-op).

Single-gate shifts all fail (mux k4, shift k6, pfifo k8/k9, writes
k2, group g_r3, and the full combination): the L0b's delay changes
the select-vs-data relationships in a way that a uniform +1 does not
capture -- the pfifo's absolute-phase window and the products'
data-relative slots move differently. Closing the DOUT-CKO (~1.4ns)
this way needs a full re-derivation of the phase windows under the
extra read register (the model = the contract), NOT guess-and-check.

Current committed state: 20/20 bit-exact, KU5P impl WNS -0.051
(36 eps, the BRAM-DOUT->c1/c3/sd paths), 20 DSPs.

## 3-DSP cmult investigation (thoughts/cmult.v) -- IN PROGRESS

The reference implements the Gauss trick with a shared term:
    common = (a-c)*d,  pr = (b-d)*a + common,  pi = (b+d)*c + common
    (= a*b - c*d, c*b + a*d EXACTLY)
3 multiplies instead of 4 -> the R2^2 stage drops from 4 to 3 DSPs
(the N=2048 core: 20 -> 15).

Verified:
- identity exact at the integer level and at the MWB/PW truncation
  level (200k random, all widths).
- RTL prototype: N=4 (D=1) BIT-EXACT via rtl_check.
- D=2 single stage: 40/40 identical to the 4-DSP RTL (direct RTL-vs-
  RTL, not model-compare).
Warnings/bugs found along the way:
- common_r <= mult0 (nonblocking) is ONE CLOCK STALE in the p sum =>
  the ALU must add the same-clock MREG directly (p_re = multr+mult0).
- Verilog '*' sizes to the WIDEST OPERAND, not the product: the
  19-bit t_diff * 18-bit m_re computes at 19 bits, losing the top;
  all multiplicands must be pre-widened to MWB (like the original
  m_re_w/tr_w).
- my diagnostic harness's stim parser (f>>hex) silently zeroed the
  input (a red herring).

Open puzzle: the D=4 stage diverges from the 4-DSP at the frame-2+
outputs with the SAME m/t operands but different p/out (D=2 is clean,
D=4+ breaks). The 3-DSP p-arithmetic is DEPTH-independent, so the
divergence smells like a downstream interaction (pfifo/y0 gate at the
16-phase period) rather than the multiply arithmetic. Needs the fresh
pass: dump the shift_p/pfifo slotting for the D=4 frame-2 in both
versions.
NEXT: solve the D=4 divergence, then re-verify the full sweep, then
synthesize (expect DSPs 20 -> 15).

## 3-DSP cmult -- ROOT CAUSE FOUND + full investigation conclusion

The D=4 divergence root cause: Verilog sizes the +/- operators to
the widest OPERAND, so `tr_r2 - ti_r2` (both 18-bit) evaluates at 18
bits -- a result like +185,364 has the 18-bit sign bit set and gets
sign-extended as NEGATIVE. Fix: widen the operands first:
  wire signed [TW:0] t_diff = $signed({{1{tr[TW-1]}}, tr}) - $signed({{1{ti[TW-1]}}, ti});
(same for t_sum and m_diff). With the fix the D=4 stage is 34/34
identical to the 4-DSP and the FULL rtl_check is 20/20 BIT-EXACT.

The synthesis does NOT give 3 DSPs: the Vivado refactors the three
Gauss products into FOUR DSPs per stage (20 total, not 15) -- the
common (a-c)*d fans to BOTH re/im C-inputs, so its product gets
duplicated (the mapping shows two (C or 0)+A*B2 and two
(C+((D'-A2)*B2)')' per stage). AND the timing degrades (impl WNS
-1.002 vs the 4-DSP's -0.051): the extra LUT adders + the cross-DSP
fanouts.

The registered-common (common_r) fanout hub is INHERENTLY one clock
stale -- the nonblocking `common_r <= mult0` re-registers the MREG,
so the p sum would pair the multr with the PREVIOUS clock's common
(verified: cycle-18's p used the cycle-16 multr with the cycle-15
common). Fixing it needs an extra pipeline hop (together with the
multr_r register), shifting all the downstream gates.

CONCLUSION: the Gauss arithmetic is proven and bit-exact (20/20),
but reproducing the reference's 3-DSP count in our RTL requires
either explicit DSP48E2 instantiation or the +1-common-pipeline
shift -- and neither fixes the worse timing (the cross-DSP/LUT-add
paths). The 4-DSP committed state (20 DSPs, WNS -0.051) remains the
best synthesizable design. The 3-DSP port is documented for the
future (a DSP-primitive effort), with AR/BR/MR/PR usage in mind.

## PRODUCTION CORE: fft_sdf_r22.v verified (P7 step 1)

The generic wrapper (rtl/fft_sdf_r22.v + fft_top_r22.v, parameters via
-G, NOT the per-config generated spike top) is now the production core
and is BIT-EXACT against R22SDFGoldenModel including tuser/tlast marker
alignment and ce/tvalid freeze masks:

  rtl_check_prod.py: 27/27 -- N=2,4,..256 fwd+inv (odd/even stage
  counts, last-pair D=1 collapse), N=1024/2048, widths 8/12.3->20/25->20,
  tw 8/10.8, scaling 000/201/222, freeze periodic/bursty.

Fixes vs the old wrapper (which was synthesis-sweep-only, never
simulated): INVERSE parameter (was hardcoded 0); per-pair K_PRELOAD
phase from the VERIFIED 3D+9 chain latency (was 3D+10); LATENCY is the
datapath latency sum(3D+9)[+11][+1 quant reg] (was N + double-count);
leftover D=1 stage gets its post-warm parity preloads
(wptr=pwp=parity, raddr=1-parity, compute=parity, pipe=0) computed in
Verilog (were tied 0 with a TODO); leftover twiddle at
pair_base(NPAIRS) (the appended W^0 word); array decls valid for
NPAIRS=0 (N=2).

NOTE for step 5: golden_ssr.py's r22 lane latency ("M + sum(3D+10)")
was written against the OLD, wrong wrapper formula and must be
re-derived (extra lane delay = 8*npairs + [leftover 0] + 1 vs the
golden's 3D+1 stages -- to be pinned against the verified RTL).

## SSR r22 PRODUCTION: generate_ssr(r22) verified (P7 step 5)

- The lane (fft_top_r22 REORDER_OUT=1) is cycle-exact vs the model lane:
  _extra = 8*npairs + 1 (RTL 3D+9/pair + 11/leftover + 1 quant reg, minus
  the golden 3D+1/pair), lane latency = core+M. The old _SSRLane
  formula (M + sum(3D+10)) was written against the pre-step-1 wrapper
  and is replaced.
- p_off = 1 for the r22 crossbar pairing (first lane-valid word carries
  position 0); empirically settled, R=2/4, M odd/even.
- config: r22 now accepts ssr>1 under the SSR native->native contract.
- fft_ssr_r22 lanes get INVERSE; lane ROM = write_r22_twiddle_mem(M)
  (conjugated w/ cfg.inverse), cross unchanged.
- tests/test_rtl_ssr_r22.py: R=2/4/8 sizes incl. the M=2 leftover-only
  lane (N=16 R=8), fwd+inv -- 6 tests/15 subtests green within the
  documented R/2+1 tolerance. r2 suite unchanged (136 total).
