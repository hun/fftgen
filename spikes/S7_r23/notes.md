# Spike S7 — radix-2³: the 45-degree (W8) rotation — timing/pipelining probe

Charter (PLAN stretch goal): estimate how the ±45° class (W^{N/8}/W^{3N/8},
√2/2 coefficients — the one thing that keeps the deepest r2³ triple's
multiplier alive) times and pipelines, BEFORE writing the golden model.

Stage-role mapping (settled up front): within a merged triple there is ONE
shared proper cmul (4 DSPs) time-multiplexing ALL product classes (~7 per
8-clock group, staggered P7-style). Per-stage specialization only appears at
the DEEPEST triple, where the slices collapse:

| sub-stage | twiddle slice | handling |
|---|---|---|
| N+0 (outer) | {W⁰, **W^{N/8}**, W^{N/4}, **W^{3N/8}**} | W⁰/W^{N/4} fabric-trivial; **±45° = the spike's subject** |
| N+1 (mid)   | {W⁰, W^{N/4}} | trivial (operand ±j fold) |
| N+2 (inner) | {W⁰} | pass-through, no multiply |

## Findings

### 1. Numeric side (rot45.py): the 45° words already exist; the fabric
rotate would only ever deviate from them

- q = round_half_up(√2/2·2^td): **5–7 non-zero taps** (td 8..18, pattern
  td-invariant, q(td+2)=4·q(td)) → ~3 CARRY8 levels of fabric constant
  multiply, the direct generalization of P6's `trivial_prod`.
- The canonical magnitude-first table already contains the eighth roots as
  ordinary words: T[N/8] = (92682, −92682), T[3N/8] = (−92682, −92682) at
  td=17 — exactly ±45.00°/135.00°. **A ROM-word 45° product IS the pinned
  canonical contract; there is nothing to re-quantize.**
- Fabric-rotate candidates vs the float ideal (N=256, tw=18, td=17):
  product-side exact rotate (B) 0.28 LSB, operand-side (C) 0.40 LSB,
  ROM-precombined T45 table (A) 0.27 LSB; pairwise ≤ 1 LSB. All inside the
  existing noise floor — the decider was always timing, not numerics.

### 2. Timing probe (probe.py, one stage D=128/16b/18b, KU5P OOC @2 ns)

| variant | placement of the ×q fabric rotate | DSP | LUT | WNS | fail EP | worst path |
|---|---|---:|---:|---|---:|---|
| `base` | none — 45° rides the shared cmul as ROM words | 4 | 867 | **MET** | 0 | tw_dout → DSP B |
| `w8_pre` | operand-side, comb. into AREG | 4 | 1140 | −1.928 | 120 | k3_reg → DSP A, 12 lvl (5 C8) |
| `w8_pre_pipe` | + own register before AREG | 4 | 1140 | −1.875 | 78 | k3_reg → DSP A (Vivado retimes the pipe register away) |
| `w8_post` | product-side after PREG, into shift stage | 4 | 1201 | −2.002 | 32 | DSP_OUTPUT → LUTRAM D, 17 lvl (10 C8) |
| `w8_post_pipe` | tap tree + own register, shift next hop | 4 | 1201 | −1.164 | 38 | **DSP_OUTPUT → tree reg alone = 3.011 ns** |

`use_dsp="no"` verified (4 DSPs everywhere — the ×q trees really stayed
fabric). Even a perfect two-way split cannot reach 2 ns: the post-DSP tree
hop ALONE is 3.0 ns (8-CARRY8 propagate chains on 55-bit operands), and the
operand side balances at 3.9 ns across the retimed registers. The only
remaining fabric escape is a hand-truncated windowed multiplier (~22-bit
result of the 55-bit product) plus a 2-extra-hop schedule — for −4 DSPs.

### 3. Verdict

**The 45° rotation rides the shared cmul as ordinary canonical-table words —
zero new hardware, timing = base = MET (the proven r22 pipeline).** The
fabric rotate (operand- or product-side, with or without a pipe register) is
REJECTED: ~3–4 ns per hop against a 2 ns budget, to make the deepest triple
DSP-free (4·⌊n/3⌋ → 4·(⌊n/3⌋−1), i.e. 12→8 at N=2048) — ≈0.2 % of the KU5P's
1824 DSPs. PLAN caveat (i) is confirmed as the correct accounting: the
multiplier stays busy to the last stage, 4·⌊n/3⌋ stands.

Corollary for the golden model:
- Contract = canonical twiddle table throughout; no separate T45 table, no
  extra quantization point for the 45° class (unlike a fabric rotate, which
  would have deviated ≤1 LSB — moot now).
- The merged triple still RE-PINS the rounding points exactly like r22 did
  (S5 finding 2): fused triple products round once at td+σ_fused instead of
  per sub-stage. Expect a few-LSB delta vs the plain r2 golden with
  identical SQNR — same re-pin procedure as P7.
- Stagger duty ~7 products per 8-clock group (≤1/clk, throughput safe);
  exact class-to-phase choreography is golden-model work.
- PLAN caveat (iii) (combine depth on the L0→CARRY8→LUTRAM-write path): the
  write-path DATA stays structurally r22's (first-level butterflies in the
  rings, 3-C8 class — S5 impl_aggr measured that class at +0.003 post-route
  with aggressive directives); the r2³ 3-level diff trees land on the
  DSP-OPERAND path, which base shows has budget. To be confirmed by the
  stage RTL once the schedule exists.

## Files

- `rot45.py` — tap structure + candidate LSB deltas (numeric contract)
- `mk_variants.py` — generates `variants/` from the S5 `nat` stage
- `probe.py`, `probe_top.v` — OOC KU5P @2 ns probe (reuses S5's
  `rtl_check.write_r22_twiddle_mem`)
- `build_*/` — Vivado runs per variant

(Lint note: iverilog cannot parse the function-call bit-selects that the
proven nat file already uses; lint with the two `round_shift_bw(...)[…]`
lines rewritten — all five variants parse clean then.)

## Pipelining plan for the r2³ stage (ternary-adder assumption, post-S7 revision)

Assumption (user directive): a 64-bit ternary adder `d <= a + b + c` meets
timing at 500 MHz as ONE logic level. This re-opens the fabric rotate that
S7 rejected (that verdict measured UNPIPELINED trees) — and working the
kernel algebra through exposes a structural fact S7's "base" missed:

**Kernel algebra (verified numerically, 500 random 8-groups).** With
y_k = the 7 non-trivial outputs of the 8-group, table twiddles
T[k·j·8^m] (k = 1..7, j = group index) and d0..d3 = the group's four
first-level diffs:

    X1 = (d0 - j·d2) + (r1 - j·r3)      X5 = (d0 - j·d2) - (r1 - j·r3)
    X3 = (d0 + j·d2) + (r3 - j·r1)      X7 = (d0 + j·d2) - (r3 - j·r1)
    r1 = W8·d1,  r3 = W8·d3   (THE only two rotates; W8 = W^{M/8})
    X2/X6 = q0 ± j·q1-fold · T[2j·8^m],  X4 = (p0-p1) · T[4j·8^m]

- The W8^{1,3} kernel constants CANNOT ride the twiddle: splitting the
  d-class operands around them costs 2 products per output → 11 products
  per 8 clocks > 8. **The operand-side rotate is structurally required,
  in EVERY triple** (S7's "45° rides as ROM words, zero new hardware"
  was incomplete — it ignored the kernel constants).
- Linearity halves the rotate cost: rot135(x) = -j·rot45(x), so 2 rotates
  per 8-group feed all four d-class outputs through free sign/swap
  combines (duty 1/4 of the product path, 2/8G on the stream).
- Consequence: at the DEEPEST triple (j = 0 → all twiddles T[0] = W⁰) the
  products ARE the operand combos → the shared cmul is idle → **DSP-free
  deepest triple**: lane = 3·4 = 12 DSPs, not 16. This reverses S7's
  "4·⌊n/3⌋ stands" — with the pipelined rotate, R=2 N=8192 totals
  2·12 + 4 = **28 DSPs** (the PLAN's own N=2048 R=2 row number, coincidentally).

**Rotate unit (3 hops, round folded into the last add).** q = 92682 =
2^{16,14,13,11,9,3,1} (7 taps). Folding the (re±im) pre-add into the tree
gives 14 shifted terms + the round bit — still 3 ternary levels:

    L1: P1 = re·2^16 + re·2^14 + re·2^13   Q1 = im·2^16 + im·2^14 + im·2^13
        P2 = re·2^11 + re·2^9  + re·2^3    Q2 = im·2^11 + im·2^9  + im·2^3
        (re·2^1, im·2^1 pass as wires)
    L2: A = P1 + P2 + re·2^1               B = Q1 + Q2 + im·2^1
    L3: re' = A + B + (1<<16)   [ternary, round-half-up folded]
        im' = B - A + (1<<16)   [ternary with negated A]
    >> 17 is pure wiring; result rounds to CB (18b) — the operand rotate's
    one extra rounding (rot45.py: 0.40 LSB, inside the contract).

8 ternary adds (~37-bit) per unit, 2 units per triple (the two rotate-class
d's can be < 4 clocks apart at small G), ~1.3k LUT + ~1.2k FF per triple.
The user's literal grouping (pre-add + 7 taps over 3 stages) is the 4-hop
fallback; the folded tree is preferred (latency matters for the pfifo window).

**Layer table (one merged triple, period 8G clocks, G = M/8^{m+1}).**

| hop | contents |
|---|---|
| L0 | registered reads: ring0 (4G, raw), ringA_s/d (2G), ringB_s/q/r/u (G), input x, twiddle DOUT (BRAM, 2-reg read) |
| L1 | first-level butterflies: sA = round(a+x, σ_{3m}) [1 C8], dA = a−x [1 C8]; write ring0/ringA at arrival; dA regs = rotate inputs |
| L2 | B-level pairing (distance 2G): pB = sA_n + sA_o, qB = sA_n − sA_o, rB = dA_n + dA_o, uB = dA_n − dA_o [1 C8 each]; write ringB at arrival |
| L3–L5 | rotate tree R1/R2/R3 (2 units, phase-gated to the rotate-class d's) → r1/r3 regs |
| L6 | C-level operand combines: d-class bases (d0 ∓ j·d2) [1 C8 from L0 reads], r-combos (r1 ∓ j·r3 / r3 ∓ j·r1) [1 C8 from L5 regs], sum [1 C8]; q-class folds qB ± j·uB; p-diff; → 7-way k-mux |
| L7 | AREG/BREG capture (operand + twiddle) — DSP input regs, as r22 `nat` |
| L8 | im-path products (MREG) + re-operand freeze (the proven stagger) |
| L9 | re-path products (MREG) + C-port regs (CREG) |
| L10 | ALU combine → PREG |
| L11 | fused shift staging (td + σ per class); y0/X0-chain alignment |
| L12 | pfifo write + output mux |

**Duty/throughput audit (per 8G period):** 7 cmul products (1 idle phase —
the k-mux is 7-way, tighter than r22's 3-way but ≤1/clk holds); 2 rotates
(units idle 7/8); 1 write/clock per ring. Binding path per product:
L1 → rotate (3) → L6 combine → L7 AREG → L8/L9/L10 DSP → L11 → L12
≈ 12 hops input→pfifo (vs r22's ~9) → **stage latency ≈ 6G + 12**
(vs r22 pair 3D+9); exact phases = golden-model derivation.

**Timing audit:** DSP internals = the proven r22 `nat` stagger, untouched.
Rotate hops = single ternary adds ≤ 38-bit — MET by assumption. Operand
path L6 is +1..2 C8 vs r22 (3-level combines) — S7 base showed budget
(its worst path was tw_dout→B). Write path unchanged (3-C8 class,
S5 impl_aggr +0.003). Watch items: (a) the pfifo window shifts +3 hops —
use the r22 lesson (per-block product-window flag tracking ready steps,
NOT a fixed phase window); (b) the X0/y0 chain needs a ~12-deep alignment
chain; (c) ring/pointer schedule (warm preloads, K_PRELOAD parity) =
golden-model work, same machinery as r22.

**Resources (R=2, N=8192, lanes M=4096, 4 triples, G = 512/64/8/1):**
DSP 2×12 + 4 = 28 (r22: 52). Rings ≈ 14G/triple (4G + 2·2G + 4·G + 2G
pfifo) → Σ 14·585 ≈ 8.2k words ≈ 262 kb/lane (r22: ~306 kb/lane); BRAM
candidates: ring0 4G = 2048 words (and 2G = 1024 borderline) per lane,
crossbar reorder + WN table as per DN-C; rotate fabric ≈ 1.3k LUT/triple
→ ~5.2k LUT/lane, ~11k total — the price of the 24 saved DSPs and the
enabler of the whole architecture.

**Next:** golden model (pin the y_k ↔ T[k·j·8^m] contract, per-class fused
shifts, the 12-hop schedule and pfifo windows), then fft_stage_r23.v
mirroring this table register-for-register.

## Inference spike: the ternary rotate tree (rot45_probe/, done)

The plan hinged on `a+b+c` chains inferring as fabric at 500 MHz. Verified:
one rot45_unit (3 hops exactly as planned: L1 4 ternary partials + delayed
2^1 taps, L2 2 ternary, L3 2 ternary with the round-half-up folded in,
`>>>TD`+truncation as D-side wiring) ×2 instances in a probe top, OOC KU5P
@2 ns:

- **DSP48: 0** — nothing leaks into DSP primitives.
- **Timing MET, worst slack +0.687 ns** (the L2 hop, i.e. the deepest);
  0 failing endpoints — every hop of both units and the output consume
  path passes with margin.
- Worst path = **5 logic levels (3 CARRY8 + 2 LUT)** = exactly ONE ternary
  add (LUT compressor level + one carry chain); no register merging, no
  multi-add paths.
- Area: 516 LUT / 488 FF / 66 CARRY8 for BOTH units + consume glue
  → ~250 LUT/unit — 4× under the plan's ~650 LUT/unit estimate
  → rotate fabric ≈ ~450 LUT/triple, ~1.8k LUT/lane at 4 triples
  (vs 4 saved DSPs in the deepest triple + the structural enabler).

Verdict: the ternary assumption holds in silicon terms — the pipelining
plan (notes above) stands. Remaining unknowns are schedule-only (pfifo
window +3 hops, ring phases), i.e. golden-model work, not inference risk.

## Golden model (DONE): batch contract + cycle-accurate streaming stage

`fft_fixed_batch_r23` + `_R23DIFStage` + `R23SDFGoldenModel` now live in
`src/golden.py`; tests in `tests/test_golden_r23.py` (suite 169 green).

**Batch contract** (verified vs plain r2: 1-3 LSB delta at 16b/18b, same
bound law as the r22 re-pin -- `max(2, 2^(W-15), 2^(18-tw))` -- identical
SQNR, fwd+inv, N=8..1024, all leftover counts):

    s_i = round(a_i+a_{i+4}, s0);  d_i = a_i-a_{i+4}        (i = 0..3)
    p0 = round(s0+s2, s1);  p1 = round(s1+s3, s1)
    q0 = s0-s2;             q1 = s1-s3                       (exact)
    r1 = rot45(d1);         r3 = rot45(d3)    -- the ONLY two rotates
    y0 = round(p0+p1, s2)
    y1 = cmul(bm+(r1-j r3), T[  j*8^m], td+s0+s1+s2)   bm = d0-j d2
    y2 = cmul(q0-j q1,      T[2j*8^m], td+s1+s2)
    y3 = cmul(bp+(r3-j r1), T[3j*8^m], td+s0+s1+s2)    bp = d0+j d2
    y4 = cmul(p0-p1,        T[4j*8^m], td+s2)
    y5 = cmul(bm-(r1-j r3), T[5j*8^m], td+s0+s1+s2)
    y6 = cmul(q0+j q1,      T[6j*8^m], td+s1+s2)
    y7 = cmul(bp-(r3-j r1), T[7j*8^m], td+s0+s1+s2)
    x[off+j+bitrev3(k)*G] <- y_k          (bitrev3 = 0,4,2,6,1,5,3,7)
    rot45(x) = (rs((xr-xi*js)*q8, td), rs((xi+xr*js)*q8, td)), q8 = 92682

**Streaming stage, FINAL schedule** (period 8G; stage latency **7G+2**;
the RTL adds a uniform operand-phase shift H -- every write->read lag is
>= 1, so any H works without touching the model):

    [0,4G):  a0..a3 -> ring0(4G); staggered y2/y6/y1 of the PREVIOUS
             block (slots [0,G) / [G,2G) / [3G,4G); [2G,3G) idle)
    [4G,5G): a4: sA_0 -> ringA_s[g] (2G), dA_0 -> ringA_d0[g] (G)
    [5G,6G): a5: sA_1 -> ringA_s[G+g], dA_1 -> ringA_d1[g] (G)
    [6G,7G): a6: p_0/q_0 -> ringB_p/ringB_q (G);
             bm/bp = dA_0 +- j dA_2 -> ringBB[g]/[G+g] (2G)
    [7G,8G): a7: p_1/q_1; y0 -> y0_reg; y4 -> pfifo[7G+g];
             q_1 -> ringB_q1[g] (G); rot(dA_3) -> unit-B pipe
    unit A:  re-reads ringA_d1 at [10G-3+g]; ringR[g] written at 10G+g
    unit B:  ringR[G+g] written at 7G+3+g
    slots:   y1/y5/y3/y7 at [3G..7G)+8G: cmul(ringBB, ringR)
    emission: latency 7G+2; member windows [(i+7)G+1, (i+8)G+1);
             member 0 = y0_reg, else pfifo[BASE[member]+g]
             (BASE: y4->7G, y2->0, y6->G, y1->3G, y5->4G, y3->5G, y7->6G)

**Lessons that shaped it (the "no rebalancing" insurance):**
- Per-group values consumed AFTER their register's natural lifetime
  (r1/r3, q1) must live in RINGS, not registers -- G > 1 groups overlap
  in flight; a hold register is clobbered after 1 clock. The r-ring
  write is phase-shifted by the 3-clock rotate pipe, so its phase->group
  map is offset by 3 (read addr (g+3)%G at the input, (g-3)%G at the
  write) -- the RTL must reproduce these +3-shifted addresses exactly.
- bm/bp are pre-combined at [6G,7G) to bound ringA_d0's lifetime (the
  d-class slots at [3G..7G)+8G would otherwise read ringA_d0 two blocks
  after its overwrite). dA_2 is never stored.
- All ring reads use pre-edge snapshots; the y5/y7 slots read rings in
  the same phase window where the next block writes them (read-old).
- Memory per triple (complex words): ring0 4G + ringA_s 2G +
  ringA_d0 G + ringA_d1 G + ringB_p G + ringB_q G + ringB_q1 G +
  ringBB 2G + ringR 2G + pfifo 8G = **22G** (up from the 14G estimate;
  the retention rings and static 8G pfifo are the delta). Lane RAM at
  M=4096: ~22*585 = 12.9k words ~ 412 kb (r22: ~306 kb/lane). pfifo can
  shrink to 7G by relabeling bases (8th slot is idle).
- Test-harness trap that cost a loop: the batch contract processes ONE
  frame per call -- multi-frame verification must slice per frame.

**RTL hop budget (declared, to mirror exactly):** ring-read capture (L0)
-> operand combine -> AREG mux -> im MREG -> re MREG/CREG -> ALU PREG ->
shift staging -> pfifo write, uniform for all 7 classes (the d-class
rotate sits BEFORE the combine, inside the +3-shifted ringR timing).
The y0 path needs a 1-deep alignment register (emission at compute+1).

## RTL: rtl/fft_stage_r23.v (WRITTEN, lint-clean; bring-up OPEN)

The stage mirrors the golden `_R23DIFStage` register-for-register with the
r22 pipeline conventions (L0 capture -> L1 combines/AREG -> L2 im MREG ->
L3 re MREG/CREG -> L4 ALU PREG -> L5 shift -> L6 pfifo write, H = 6).

Structural decisions baked in:
- Static per-class addressing everywhere: pfifo addr = cls_base + g
  (bases y2->0, y6->G, y1->3G, y5->4G, y3->5G, y7->6G, y4->7G), twiddle
  ROM addr = ROM_BASE + k*G + g (the mem writer lays out T[k*g*8^m]
  there), all the value rings are 1W1R at addr g.
- Ring widths: ring0 = IW (raw inputs), ALL value rings = CB
  (WIDTH+2) -- |rot(dA)| <= sqrt2*2^W fits CB; the product operands are
  OB = WIDTH+4 (3-term d-class combos reach ~4.3*2^W).
- Rotate units: 3-stage pipelined ternary trees (R1 = taps 0-2/3-5
  partials + the 7th tap, R2 = A/B sums gated, L3 = final add with the
  round folded in, >>>TD slice). ringR writes: unit A at model 10G+g
  (gate (k-2G) mod 8G < G), unit B at 7G+3+g (G==1, L1-comb input) or
  7G+4+g (G>1, registered input) -- the +3 pipeline hop shifts the
  phase->group map by 3 exactly as in the golden (read addr (g+3)%G,
  unit-B write addr (g-3)%G).
- k1-gated ring writes with data = L1 comb (p_0/q_0/bm/bp/q_1 -- the
  r22 sram pattern); the emission/shift/pfifo gates use k6/k5/k6.
- Q8's tap positions extracted by a constant function; popcount != 7
  configs are rejected ($error) -- the generator constrains TD.
- y0 alignment chain = 6 x CB regs, tap [4] at the emission mux.

Known-open for bring-up (next session, the r22 flow):
1. `write_r23_twiddle_mem` (ROM layout: 7 slices x G words per triple).
2. Single-stage Verilator TB vs `_R23DIFStage` (per-position compare,
   skip = latency-1; expect the off-by-one hunt on the emission/rotate
   windows exactly like the S5 L0-retiming).
3. K_PRELOAD chaining semantics for multi-stage chains (upstream
   latency = sum(7G+2+H) -- the model's pos-up convention).
4. Vivado probe (dsp_probe-style): DSP absorption (AREG/BREG/MREG/CREG/
   PREG), the rot trees in fabric, WNS @2ns.

## Synthesis probe runs (rtl_probe/) -- timing status and fixes applied

Probe: one fft_stage_r23 (G=128, 16b/18b) OOC KU5P @2ns (the S5 dsp_probe
pattern; probe.py + probe_top.v + gen_mem.py -- the ROM layout is the
interleaved 8G-word form word[base_k+g] = T[k*g*8^m], base_k = the class
bases).

| run | state | DSP | LUT | FF | WNS | finding |
|---|---|---:|---:|---:|---|---|
| 1 | first cut | 4 | 3220 | 1028 | -2.555 | ALL failing paths = the **y4 class**: ring0 DOUT -> sA_3 -> p1 -> c4 (3 chained CARRY8) -> AREG -> MREG -> CREG in ONE clock (13 lvl). The y4 combine needed pipelining. |
| 2 | y4 pipelined (sA3_r/p1_r, late AREG, k8-gated y4 pfifo write) | 4 | 41815 | 29877 | -1.893 | the pfifo grew a SECOND write port (the normal k7 + the y4 k8) -> 2W+1R does not infer as RAMB/LUTRAM -> the pfifo collapsed to FLIP-FLOPS (1024x16x2 = the 30k FFs) + the write-decode logic (the 41k LUTs). FIX: merge into ONE write port (the gates are disjoint). |
| 3 | pfifo single port | 4 | 2874 | 1096 | -2.013 | the remaining failure family: ringR1 (BRAM) DOUT -> c1 = bm+r1+jr3 (2 chained 20-bit adds) -> mux -> DSP A. The tools absorbed BOTH declared read stages into the BRAM's ADDRA+DOA, leaving the DOUT to drive the combine combinationally. |
| 4 | 3-stage reads (r1/r2/r3 -- one MORE stage than the primitives can absorb, per the user directive) + the sync-read coding | 0 | 104 | 224 | MET | **the design PRUNED to a trace** (constant propagation removed the DSPs and the rings as "unreachable") -- a connectivity break from the line-based edits (the known suspects: the butterflies still consuming r2 in one spot, the p1_r/c4 wiring, the out_im tap skew -- all since fixed but the prune persists). Simulation shows the outputs ALIVE (421 changes/1200 clocks) -- the classic sim-alive/synth-pruned mismatch. |

**Root-cause findings so far (the user's directives, confirmed):**
- The ring reads were coded as ASYNC-read wires (`wire w = mem[addr]` +
  the capture register) -- an async read CANNOT map to BRAM, so every
  ring forced LUTRAM regardless of ram_style ("automatically implemented
  using LUTRAM. BRAM implementation would be inefficient" -- at 512/1024
  deep = the mux trees the user warned about). FIXED: the L0 captures now
  read the memories DIRECTLY in the clocked block (the sync-read coding).
- The pfifo 2-write-port collapse: NEVER give a memory two separate
  write statements -- merge into one port when the gates are disjoint.
- MEM_STYLE as an untyped string parameter used in `(* ram_style =
  MEM_STYLE *)`: Vivado IGNORED it (the rings went LUTRAM); the literal
  attribute works for the pfifo (the "Infeasible attribute" warning
  proves it attaches) but the ring declarations... the 128x18 rings are
  mapped to LUTRAM by Vivado's auto-heuristic ("BRAM implementation
  would be inefficient" -- Vivado's own threshold disagrees with the
  >32-entry rule; needs the attribute honoured or a wrapper decision).

**OPEN (next session):** the constant-propagation prune of the compute
chain. The diagnostic sequence: (1) verify the emitted netlist's
connectivity around c_c/p_re, (2) check the twiddle hold chain depth
(tr_w = tw_h2 is 1 clock short for the 3-stage read -- the BREG captures
class(w+1) instead of class(w): tw_w should be tw_h3), (3) the rot
window conditions ((k+2)/(k+1) forms) need the bring-up diff, (4) the
Verilator single-stage check vs _R23DIFStage with the per-cycle compare
-- the S5 playbook finds this class of bug mechanically.

## TIMING SPIKE RESULT: the r23 stage CLOSES at 2.0 ns (KU5P, OOC)

probe.py, one fft_stage_r23 lane (G=128, IW=16, TW=18):

  DSP=4  LUT=2796 (1708 logic + 1088 mem)  FF=1960  CARRY8=198
  RAMB18=13  **WNS = MET @ 2.0 ns, 0 failing endpoints**

  -> R=2 N=8192 (7 lanes) projects to ~28 DSP / ~20k LUT / ~14k FF /
     ~90 RAMB18 per 2-stage core, at 500 MHz. The r22 stage needed
     52 DSPs; the r23's DSP-free deepest triple is what buys the drop.

The three structural fixes that got from -2.555 to MET (all per the
"give the tools register stages to absorb" directive):

1. **k-chain break** (the prune root): the k7<=k6/k8<=k7 assignments
   were dropped in a scripted edit -- k7/k8 froze at the reset value,
   cls_w/t_e/w_pf went constant and Vivado pruned the whole compute
   chain. Lesson: after scripted multi-line edits, DIFF THE SEQUENTIAL
   BLOCK, not just the declarations (lint passes; sim stays alive
   through the y0 branch).

2. **Async-read wires force LUTRAM**: `wire w = mem[a]` + a capture
   register cannot map to BRAM even with ram_style="block". All ring
   L0 captures now read the memory directly inside the clocked block
   (the sync-read coding) -> 13 RAMB18, fabric r2/r3(/r4) stages.

3. **One adder per clock into any primitive**: every 2-adder combine
   feeding a RAM write port or a DSP AREG was pipelined:
   - the second-level ring writes (p0/q0/bm/bp/q1) moved to w+4
     (k4-gated), consuming registered fresh combs (sA_r, jmdA_r) and
     4th read stages (as_r4/ad0_r4/bp_r4); the ringA_s write addr got
     the matching (k-3) group shift
   - the L1 class combs (c1..c7, 3-input adds) got output registers
     (cXr), moving the normal-class AREG to w+4 -- the SAME clock as
     y4's, which collapsed the special-case phasing: both BREGs take
     tw_h4, the pfifo write is w+9 for every class, and the normal/y4
     write windows became disjoint so the single write port survived
   - the y0 alignment pipe now carries {im,re} pairs (the im half was
     missing) and taps [5]
   - the twiddle hold chain needs tw_h4 for BOTH BREGs (tw_h2 was 2
     clocks short)

OPEN for the bring-up (value bugs the timing rework could not check):
- the addr conventions were re-derived from the golden schedule during
  the rework; the rot-unit windows (w_rotA_r1/R2, g_a3 = +4, w_r1w/w_r3w,
  g_r3 = -6) were NOT re-derived and are suspect: hand-checking suggests
  the ringA_d1 read remap should be +6 not +4 for the 3-stage read --
  the Verilator diff vs _R23DIFStage must cover the ringR outputs
  (y1/y3/y5/y7) specifically
- the p1_re_c 2-adder path into p1_r is the deepest fabric-only path
  left (~9 lvl, passing) -- watch it in a full-core context
- G<5 corner cases: the w+4 reads need the writes to be G+ clocks older;
  tiny-G configs need the LUTRAM/register fallback paths re-derived

## BRING-UP: the r23 stage + the 2-stage chain are BIT-EXACT vs the golden

Harness: spikes/S7_r23/rtl_bringup/ (bringup.py = one stage vs
_R23DIFStage; bringup2.py = the m=0->m=1 chain; tb_watch.v = the
hierarchical-signal watch used to isolate every bug). Method: dump both
streams, scan the latency offset H scoring only nonzero golden
positions, per-(block,member) census, then watch the RTL internals at
the failing phase.

### Single stage: 100% bit-exact
fwd+inverse, DEPTH in {8,16,32,64,128}, up to 8 blocks, H=8.
Five RTL bugs found and fixed:
1. **DSP cross-term**: prod_im_re was m_r_re*tr_r (the ac term again);
   p_im = ad + bc needs m_r_im*tr_r -- the verified r22 had it right.
2. **tw_h4 never loaded**: the hold-chain update tw_h4<=tw_h3 was
   missing -- y4's BREG captured 0 -> every y4 product was 0.
3. **rot unit A read map**: g_a3 = (g_addr+5)%G, NOT +4/+6. Key insight
   (empirical): the golden's rotA2 is snapshotted BEFORE the pipe
   shift, so ringR[x] = rot(d1(group x)) -- the golden's "+3 map" and
   the 3-deep pipe cancel; the RTL's write@2G+x consumes the comb
   @2G+x-2 whose r3 read was addressed at 2G+x-5.
4. **rot unit B index**: g_r3 = -6 was already correct (a -7 attempt
   based on a mis-derivation was reverted -- the same pre-shift
   snapshot cancellation gives ringR[G+x] = rot(dA_3(group x)).
5. **dA_r3 capture gate**: k4 -> k3. With k4 the first dA_3 (group 0)
   was never captured (dA_r3@7G+4 = 0) -> ringR[G+0] = 0 -> the y1/y5/
   y3/y7 of group 0 wrong by the missing r3 term (found as ±opposite
   deltas on the y1/y5 pair).

Watch out: k is a mod-8G counter -- block-N windows must be gated on
the TB clock count, not on k. And the golden's T(0) = 2^td - 1 (the
18-bit clamp) makes every g=0 product round to ~0: impulse tests are
useless for the product paths; use full-scale random data.

### Two-stage chain: 100% bit-exact
N=1024 shape: stage 1 (G=128, K_PRELOAD=0) -> stage 2 (G=16).
- **K_PRELOAD chaining rule**: stage n+1's preload =
  -(sum of upstream golden latencies + upstream H sum + 1) mod 8G_{n+1}
  = -(898 + 8 + 1) mod 128 = 117 (the +1 = the inter-stage register
  handoff: stage n+1 samples stage n's registered output one edge
  later).
- Total output latency H accumulates: 17 = 2*8 + 1.
- fwd+inverse, 6 blocks: 6144/6144 positions.

### Remaining gaps
- G <= 4 (first triples of N=8/16/32): the w+4 second-level writes and
  the 4-stage reads need >= 5 clocks of window separation; the golden
  is fine but this RTL micro-architecture is not. Options: a small-G
  pipeline variant (fewer read stages -- timing is easy there), or
  route N<=32 to the r22 core. G=8..128 all validated.
- The 3-stage chain + r2 leftover stages untested (the K_PRELOAD rule
  should generalize; the leftovers use the r22 parity preload).

## 3-STAGE CHAIN (the N=8192 triple set): bit-exact, first try

bringup3.py + tb_chain3.v: G=1024 -> 128 -> 16 (m=0,1,2), the exact
triple set of the production N=8192 R=2 core (3 triples + 4 r2
leftovers; the golden's default 4-triple split would need G=2 -- use
the 3-triple + leftovers split instead).

- K_PRELOADs from the validated rule, no tuning: stage 2 = 1013,
  stage 3 = 106 (i.e. -(7170+8+1) mod 8192 and -(7170+898+16+2) mod 128)
- total H = 26 = 3*8 + 2 (8 per stage + one handoff register between
  stages)
- fwd + inverse, 2 blocks (16384 clocks): 16384/16384 positions each.

The core-level phase/chaining design is now fully de-risked. What
remains for the production core: the wrapper (fft_top_r23.v) wiring
3x fft_stage_r23 + the r2 leftover stages (rtl/fft_stage_r22.v with
the r22 parity preload for D=8,4,2,1), the full-core sim vs
R23SDFGoldenModel-equivalent (chained triples + _SDFStage leftovers),
and the multi-stage timing sweep (the stage-2/3 G=128/16 instances
have LUTRAM-scale memories -- check their timing separately).

## fft_sdf_r23.v wrapper + the full-core integration findings

rtl/fft_sdf_r23.v: 3x fft_stage_r23 (G=N>>3, N>>6, N>>9; K_PRELOADs
from the validated rule, emitted as constant functions) + 2x
fft_stage_r22 leftover pairs (D=N>>11, N>>13) + the AXI framing
(copied from fft_sdf_r22). Lint clean.

Full-core sim (bringup_core.py, N=8192, 2 blocks): the 3-triple
section verified IN the wrapper (a twin-stage experiment: a
standalone G=1024 stage vs the same stage wired as the core's first
triple -- 0 output differences over 25k clocks; ringR contents
identical to the golden). Two integration bugs found and fixed:
1. q8_of(TWIDDLE_DECIMAL) had a 2x error (sqrt2 vs sqrt2/2 -- Q8 was
   185364): the core's rot outputs were ~pm1 garbage. Fixed; also
   validated q8_of(17) = 92682.
2. The r22 leftover ROMs must be NPTS-sized (N words, the pair's 3
   slices at [0,3D), ROM_BASE=0) -- a 3D-word file leaves the rest X
   and poisons every product.

REMAINING (the last core gap): the leftover tail's rounding contract.
The r22-pair leftovers differ from the golden's _SDFStage (P5 plain-r2)
leftovers by pm1 LSB on ~20% of outputs (round-half placement differs
between the merged r22 pair and the plain r2 stages -- both valid
fixed-point contracts). Two options:
  (a) keep the r22 pairs, and make the golden's leftovers r22-pair
      models (the core then matches the r22 rounding contract; the
      batch reference needs the same tail) -- fewer instances;
  (b) switch the tail to 4x P5 fft_stage (r2, D=8,4,2,1) which matches
      _SDFStage bit-exactly -- but their post-warm preload state
      (wptr/pwp/raddr/pipe) must come from the generator (the P5
      fft_preloads.vh pattern), and the non-trivial twiddle pipe
      registers make PIPE_PRE insufficient for D>1.
Recommend (a): the r22 pair golden models already exist inside
R22SDFGoldenModel's structure; extend R23SDFGoldenModel to take a
triple count and model the tail as r22 pairs, then re-run
bringup_core expecting bit-exactness.

## Full-core debug state (in progress, harness ready)

bringup_core + tb_core now have a T1DIFF trace: a standalone G=1024
stage (u_sa) runs next to the core, its output compared live to
u_core.t1_re. FINDINGS (fixed-q8 build, seed 424242, N=8192):

- The core's t1 matches the standalone (and the golden) for block 0
  and for the y0/y4/y2/y6 slots of every block.
- The core's t1 DIFFERS exactly at the d-class output slots
  (phases k in [3G+10, 7G+9] mod 8G -- i.e. the y1/y5/y3/y7 outputs)
  of every block >= 1: 8192 diffs, contiguous runs of 4096.
- The standalone stage with the SAME stimulus is 100% correct.

Isolated checks that PASSED inside the core environment:
- ringR write contents correct (rr1[i]/rr3[i] == the golden's
  ringR[i]/ringR[1024+i] for i in 0..7, dumped at c=12290)
- bm/ringBB reads correct at the block-1 y1 comb
- r1_r3/r3_r3 reads at SOME combs correct (e.g. 17996/337 =
  the golden's ringR[3]/ringR[1027]) but at the ringR indices near
  the window boundary (~1021-1023) the reads show +-1 garbage while
  the golden has full-scale rot values there.

HYPOTHESIS: the rot unit A's read map (g_a3 = +5) is correct for the
bulk of the window but wrong at the window edges (the golden's rotA2
snapshot vs the RTL's 3-deep pipe disagree by one rotation of the
ring at the [2G,3G) window boundary). The d1 values read for the
LAST/first few ringR indices come from the wrong block. Fix approach:
dump the golden's ringR[1018..1023] vs the RTL's rr1[1018..1023] after
block 1's rot window, derive the exact edge behavior, and adjust the
g_a3 constant or the R1/R2 window gates (w_rotA_r1/r2) so the edge
indices match. The y0/y4/y2/y6 paths and both r22 leftover pairs are
already verified bit-exact (bringup_lpair/bringup_ltail: 11798/11798
and 11786/11786 with the wrapper's computed KPs 10 and 1 -- the r22
convention needs NO handoff trim, unlike the r23 stages' +9j rule).

Harness files: bringup_core.py (full core), bringup_lpair.py /
bringup_ltail.py (the isolated r22 tail), tb_core.v (the T1DIFF +
t1/t2/l0 tap dump), tb_rotcmp.v (the twin-stage compare), tb_lpair.v /
tb_ltail.v.

## FULL CORE BIT-EXACT (both directions) -- 2025 session 2

Root causes of the wrapper divergence, in order found:

1. **q8_of 32-bit overflow** (rtl/fft_sdf_r23.v): `46341*(1<<17)` =
   6,074,007,552 overflows Verilog's 32-bit integer -> q8_of(17) =
   27146 instead of 92682. Fixed with `46341 << (td-16)` for td>=16.
2. **`.Q8(0)` on all three triple instances**: fft_stage_r23 has no
   Q8==0 self-compute convention; Q8=0 zeroes every rotator tap (and
   iverilog only warns on the popcount $error). The Q8=0 garbage also
   had popcount 7, so the tap check passed by luck. Fixed to
   `.Q8(q8_of(TWIDDLE_DECIMAL))`.
3. **Leftover-pair KP trims = +3 each** (KP_L0_TRIM/KP_L1_TRIM = 3,
   now the wrapper defaults). Empirically calibrated: the r23->r22
   handoff absorbs one clock per upstream r23 triple, unlike the
   r23->r23 +9j rule. Deriving it from latency bookkeeping proved
   unreliable; scan_l0.py (l1 tap vs golden pair-0 stream, 16 trims)
   found L0T=3 uniquely, then bringup_core L1T scan found 3.

Results (N=8192, NBLK=2, seed 424242, SCALING_PACK=32'h01555555):
- INV=0: H=43, 16384/16384 bit-exact
- INV=1: H=43, 16384/16384 bit-exact
- AXI framing verified: tvalid for exactly 16384 words, tuser at
  valid#1/#8193, tlast at valid#8192/#16384 (H=43 = golden offset;
  first data at c=8239, LATENCY=8240 aligns the depth-LATENCY marker
  shift registers exactly)

Debug infrastructure added: tb_core.v has a live T1DIFF compare
(standalone G=1024 u_sa vs u_core.t1, plus input INDIFF check), a
l1 tap (pair-0 output) in taps.hex, and tuser/tlast position dumps.
scan_l0.py sweeps KP_L0_TRIM against a golden pair-0 reference.
Key lesson: when comparing hex dumps vs Python golden tuples, mask
with & 0xFFFF on BOTH sides (raw negative ints never match).

Remaining known limitation: G<=4 first triples (N<=32) still route
to r22 or need a small-G r23 variant.

## Full-core timing campaign @ 2 ns (in progress) -- session 3

probe_core.py (rtl_probe/) synthesizes AND places+routes the full
fft_sdf_r23 OOC on xcku5p-ffva676-1-e. Journey so far:

1. First probe: WNS -0.406 / TNS 10878 -- all k->pfifo LUTRAM
   write-enable decode (the 8G-deep flat pfifo = 128 LUTRAM chunks,
   the chunk select = the cls_base adder's high bits, 5 levels).
2. BRAM attempt: ram_style=block was already present but Vivado
   declared BRAM "infeasible" (Synth 8-6849). Empirical bisect
   (cutA..cut14 in /tmp/pfifo_bisect): the trigger is the COMBINATION
   of the ring zero-init loops + the y0-select mux inside the clocked
   block. BRAM mapping worked (LUT 13938->6709) but the 2x RAMB36
   CASCADE clk-to-out (~1.37 ns) cannot cross the y0-select mux into
   the next stage: synth -0.378, post-route -0.613. ABANDONED -- the
   emission read mux after a BRAM is unaffordable at 2 ns.
3. Final structure (bit-exact, H=46):
   - pfifo stays LUTRAM but SPLIT into 7 per-class G-deep arrays
     (pf1..pf7 = y2,y6,y1,y5,y3,y7,y4) -> the write chunk select is
     the raw k9 group bits (no base adder) and the read is a 16:1
     chunk mux per class + the 8:1 member select.
   - the WRITE is precomputed one cycle early (cls_r/g_r/w_pf_r/pf_d
     registers; the write lands at w+10 instead of w+9) so the LUTRAM
     WE decodes from registers.
   - the emission READ moved +1 too (k10-based t_e/g_e/mm, y0_pipe
     tap 6): the y1/y5/y3/y7/y4 classes sit at write->read lag
     EXACTLY 1, so the write shift alone would break them; shifting
     both preserves every lag. Stage latency +1 -> trip_rtl_lat
     7G+11, trip_kpre +10/stage, wrapper LATENCY 8243, H=46
     (bringup3 rule: acc += LATS[j] + H + 2).
   - the async reads are separate wires (pfN_rd = pfN_re[g_e]) so the
     LUTRAM read address is independent of the write address.
   All bring-ups re-verified bit-exact (single stage, 3-chain, full
   core INV=0/1 at H=46).

4. REMAINING violations (post-route WNS -0.533 / TNS 5228):
   a) u_t0: g_r_reg -> out_re_reg, 7 levels, 1.77 ns ROUTES: Vivado
      maps the per-class LUTRAM as RAM64M-style with a SHARED
      address port (addr = w_pf_r ? g_r : g_e mux) -- the write
      address leaks into the read path. RAM64X1D (true dual-address
      LUTRAM) is the wanted primitive; Vivado's width packing
      prefers RAM64M (3 bits/primitive) + the address mux. UNSOLVED.
      NOTE: a shared port is fundamentally impossible anyway -- the
      async read during a write cycle must see the OLD content one
      cell OFF from the write address (lag-1), so the addresses MUST
      differ; the fix is either forcing RAM64X1D inference or the
      +2 registered-emission cascade (every path registered, stage
      latency +2: trip_rtl_lat 7G+12, trip_kpre +11, trims rescan).
   b) u_t2 (G=16): bp_r4 -> DSP A/B operand, 4 levels; x_r3 ->
      ringA_d0 write data, 6 levels -- pre-existing paths that were
      hidden below the pfifo failures; the class-combine (3-operand
      add) into the DSP operand needs one more pipeline stage, and
      the ring write data path needs retiming. Same structure
      presumably marginal in u_t0/u_t1 too (hidden by placement).

NEXT: (i) try to force RAM64X1D inference (single-bit aux array?
different coding?) or take the +2 cascade with the constant
recalibration (harnesses make it mechanical); (ii) retime the u_t2
class-combine -> DSP operand path; (iii) re-probe.

## The output-FD-stage fix (per the >32-deep RAM rule) -- session 3 cont.

Directive applied: any RAM deeper than 32 gets an output FD stage in
the pipeline. Implemented as per-class READ REGISTERS (em1..em7) plus
a registered member select (mm_r) between the LUTRAM read and the
emission mux:

    emN <= pfN_re[g_e];  mm_r <= mm;
    case (mm_r) ... out_re <= emN_re ... default: y0_pipe[7]

- the RAM read path now ends register-to-register: the RAM64M
  shared-address mux + the async read + the 16:1 chunk mux all fit
  inside one short hop (logic 0.68 ns total), and the member mux runs
  FF->FF. The g_r leak is harmless -- the read address g_e = g_r - 2
  is recomputed locally per chunk.
- the emission lands one cycle later -> stage latency +1:
  trip_rtl_lat 7G+12, trip_kpre +11/stage, LATENCY 8246, H=49
  (bringup3 rule: acc += LATS[j] + H + 3). The leftover-pair trims
  STAYED 3/3 -- they are invariant to upstream triple-latency shifts
  (the pair KP tracks the clock; only the r23->r22 convention
  difference is trimmed). y0_pipe extended to tap 7.
- ALL bring-ups re-verified bit-exact: single stage (H=9), 3-chain,
  full core INV=0/1 at H=49.
- probe (synth+route): WNS -0.533 -> -0.310, TNS 5228 -> 450 ps, the
  u_t0 pfifo paths GONE.

REMAINING (-0.310, ~6 endpoints, all one structure): the rot-B /
ring-write path in u_t1/u_t2 -- x_r3 -> dA subtract -> per-class
shift mux -> ringA_d1/rbbp BRAM write data (5-6 levels) and x_r3 ->
jmdA capture (7 levels). Fix = register dA/jmdA one more cycle
before the shift/combine (the affected ring-write schedule shifts
+1; the k-gates move from k3 to k4 for those rings) -- mechanical,
same pattern as the pfifo fix.

## Remaining -0.310: the dA subtract -> ring-write path (next step)

The last failing structure (post-route, ~6 endpoints, all one shape):
x_r3 -> dA_re_c = a0_r3 - x_r3 (an 18-bit subtract = 3x CARRY8 chain,
1.86 ns data) -> per-class shift mux -> ringA_d0/d1/rbbp BRAM write
data (5-6 levels); and x_r3 -> the jmdA_r capture (7 levels).

Fix (mechanical, same pattern as the pfifo qN stage):
1. register the subtract output: dA_w <= dA_re_c (and p1_w <= p1_re_c
   if it shows up too);
2. the ringA_d0/d1 writes consume dA_w with their gate/address
   shifted +1 (d2_a4/d2_a5 -> registered versions, g_w2 -> g_w2_r);
3. jmdA_r captures from dA_w (+1) -- its consumers (the rbbp
   combine at w+4) then shift +1 as well (the 4th read stages are
   already registered; check whether the bm/r1/r3 read registers
   need the extra cycle too);
4. re-verify the full chain (the ring-write schedule shifts touch the
   golden tolerance only via uniform H -- the per-stage extra grows
   by 1 again: trip_rtl_lat 7G+13, trip_kpre +12, trims 3/3, H +3).
Do NOT rush: the rbbp/rbbm combine path shifts mean the bm write and
its read schedule must move together.

Alternative quick win if the routes dominate: the 1.09 ns of routing
on the subtract->BRAM path suggests placement spread; a pblock (or
netlist-level check that the CARRY8 and the BRAM are adjacent) may
close it without RTL changes. Try the probe with the dA subtract
constrained before resorting to RTL.

## TIMING CLOSED: full fft_sdf_r23 @ 2 ns, WNS +0.028 -- session 3 final

The dA-subtract fix (registered one stage earlier, zero schedule
change): dA_f <= a0_r2 - x_r2 -- since a0_r3[C] = a0_r2[C-1], the
registered dA_f[C] is bit-identical to the old comb dA_re_c[C]; every
consumer (the ringA_d0/d1 writes, the jmdA capture, the dB mux) reads
the register. No H/KP changes.

The w6/w7 combines got the same treatment: the write data (ringB_p/q,
rbbm, rbbp) registered one cycle ahead (ringB_p_w <= rsh_cb(as_r4 +
sA_r, SIGMA1) etc -- the rounding folds into one adder), with the
gates/address shifted +1 (w6_r/w7_r/g_w4_r). The ring reads are
untouched so every lag grows by exactly 1; the output timing is
unchanged (H stays 49, no constant changes).

FINAL probe (synth + place + route, xcku5p-ffva676-1-e @ 2 ns):
  TIMING MET, WNS +0.028, TNS 0, 0 failing endpoints
  DSP48 20, CARRY8 642, RAMB18 57, RAMB36 16, FF 8640
Bit-exact: single stage (H=9), 3-chain, full core INV=0/1 (H=49).

The design rule set is now: every RAM deeper than 32 gets an output
FD stage; every RAM write data path is a single register hop from a
pipeline register; every comb feeding a primitive is at most one
adder deep. All schedules derive from the k-chain delays.

## Datasheet sweep for r23 (build/datasheet_r23)

src/datasheet_sweep.py gained the r23 arch (TCL_R23 + artifacts_r23:
the 3 triple ROMs 8*G words + N-sized leftover-pair ROMs, pack = 1
shift per stage, INTERN_WIDTH=16). Swept N = 256..32768, R=1:

- synth ok: N = 512, 2048, 8192, 32768 (post-synth WNS -0.09..-0.22,
  DSP 12-20, LUT 5.4-8.2K; N=8192 matches the verified post-route
  +0.028).
- synth FAIL: N = 256/1024/4096/16384 -- the parity check
  (NSTAGES-9 must be even; the r22 leftovers come in pairs).
- functional status of the synth-ok N (NOTED, root-cause later):
  - N=8192: fully verified (bit-exact fwd+inv, post-route +0.028).
  - N=2048: expected BROKEN -- third triple G=4 hits the small-G
    window-separation limitation (ringB_p/q1 lag = G-5 < 0).
  - N=512: BROKEN -- third triple G=1; PLUS lpair_rtl_lat(0) is
    unguarded in LATENCY (NPAIRL=0 adds 9 phantom clocks) and
    lpair_kpre does %0 (unused garbage).
  - N=32768: BROKEN -- NPAIRL=3 > the 2 hardcoded pair slots, stages
    14-15 silently dropped (truncated transform); needs a third pair
    slot + TWIDDLE_FILE_L2.
- working-N set today: N=8192 only. Extension paths: parameterize the
  pair count; a small-G r23 variant (or route the small triple to
  r22); a leftover-parity scheme for odd NR2.

## NTRIP/NPAIRL parameterization: 5 of 8 N now bit-exact

rtl/fft_sdf_r23.v refactored: NTRIP auto-derived (the largest t in
1..3 with 3t <= NSTAGES, (NSTAGES-3t) even, and the smallest triple
G = N>>(3t) >= 8 -- the exact small-G boundary since G values are
powers of two), NPAIRL generated as a loop (0..4+) with ONE
concatenated leftover ROM (the pair jj's 3*D slice at
pair_rom_base(jj), NPTS = LROM_WORDS), the trims default to NTRIP
(KP_L*_TRIM = -1 = auto), the latency totals looped (the NPAIRL=0
phantom-clock bug fixed), lpair_kpre %0-guarded.

Verified bit-exact (bringup_core.py, now N-parameterized):
  N=512   (NTRIP=1, NPAIRL=3) INV=0 H=35
  N=1024  (NTRIP=2, NPAIRL=2) INV=0 H=38
  N=4096  (NTRIP=2, NPAIRL=3) INV=0 H=46
  N=8192  (NTRIP=3, NPAIRL=2) INV=0/1 H=49  (regression)
  N=32768 (NTRIP=3, NPAIRL=3) INV=0 H=57
The trim=NTRIP hypothesis CONFIRMED (NTRIP=1 and 2 verified).

KNOWN REMAINING (noted, root-cause next session):
- N=2048 INV=0: 96.88% -- 128 bad. Bisected: the r22 stage at D=64
  has exactly 2 bad positions (the internal k=121,185 = the y3 slot
  and the a2 slot of the SAME group 57 = D-7, consecutive periods),
  propagating x4 per pair (2->8->32->128). D=64 was never verified
  before (the N=8192 pairs are D=4,1; the r22 core swept D=16,4,1).
- N=512 INV=1: 96.88% -- 32 bad. Bisected: the r22 stage at D=16
  INV=1 has exactly 2 bad (the internal k=27,43 = the y3/a2 slots of
  the same group 11), propagating x4 x3 pairs. The D=16 INV=0 is
  clean; the D=4/1 INV=1 are clean (the N=8192 regression).
- Pattern: the r22 stage has a 2-position schedule bug for
  (D=16,INV=1) and (D=64,INV=0) -- the size/parity combos never
  exercised. The failing slots: the y3 product of group g=D-5/-7 and
  the a2 write of the same group in the next period.
- N=16384 would use the D=64 first pair (NTRIP=2) -> expected broken
  until the D=64 bug is fixed. N=256: no valid triple count (the
  small-G + parity) -- documented, needs the small-G variant.
- debug infra: the wrapper now has dbg_p0..p3 alias wires (iverilog
  does not resolve the hierarchical wire-array element refs
  reliably); tb_core is N-parameterized (NUM_POINTS/NBLK/PACK).
