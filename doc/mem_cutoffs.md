# Memory style cutoffs and synthesis evidence

Empirical backing for `mem_policy.py` decisions (PLAN.md 2.7). Every `auto`
constant must trace back to a measurement recorded here.

---

## S1 — BRAM inference of ring-buffer codings (spike, P2)

**Question.** Does Vivado infer block RAM (rather than LUTRAM / extra logic)
for the SDF delay-line access patterns, specifically the read-old/write-new
same-address trick that enables two-lines-per-BRAM pairing?

**Method.** Minimal OOC synthesis (`synth_design`, no constraints), three
functionally-equivalent delay-line codings + one deep variant:

| coding | structure |
|---|---|
| `sdp` | port A write / port B read, shared pointer (baseline) |
| `rw1` | **single port**: read-old + write-new at the same address |
| `tdp_pair` | **two independent delay lines**, one per port of one TDP array (the pairing heuristic) |
| `sdp_deep` | SDP with independent read pointer, `ram_style="ultra"` |

All use nonblocking assignment order read-then-write (read-before-write),
one output register, clock-enable on every register. Sources:
`spikes/S1_bram_inference/rtl/`. Vivado 2026.1, out-of-context,
`-generic DEPTH/WIDTH`.

### Results

Artix-7 `xc7a100tcsg324-1`:

| coding | geometry | LUT as Memory | RAMB36E1 | RAMB18 |
|---|---|---|---|---|
| sdp      | 1024×32 | 0 | 1 | 0 |
| rw1      | 1024x32 | 0 | 1 | 0 |
| tdp_pair | 1024x32 | 0 | **1** (two lines!) | 0 |
| sdp      | 64x32   | 0 | 0 | 1 |
| rw1      | 64x32   | 0 | 0 | 1 |
| tdp_pair | 64x32   | 0 | 0 | 1 |

UltraScale+ `xcku3p-ffva676-1-e`:

| coding | geometry | LUT as Memory | RAMB36E2 | URAM288 |
|---|---|---|---|---|
| sdp      | 1024x32 | 0 | 1 | 0 |
| rw1      | 1024x32 | 0 | 1 | 0 |
| tdp_pair | 1024x32 | 0 | **1** | 0 |
| tdp_pair | 8192x72 (`block` hint) | 0 | 16 | 0 |
| sdp_deep | 8192x72 (`ultra`) | 0 | 0 | **2** |
| rw1_ultra | 8192x72 (`ultra`) | 0 | 0 | **2** |
| tdp_pair_ultra | 8192x72 (`ultra`) | 0 | 0 | **2** |
| tdp_pair_off | 1024x32 (`block`) | 0 | **1** (both parts) | 0 |
| tdp_pair_off | 8192x72 (`block`) | 0 | 16 | 0 |

Raw reports: `spikes/S1_bram_inference/build/<coding>_<geom>_<part>/util.txt`.

### Findings

1. **(b) infers cleanly**: single-port read-old/write-new maps to RAMB36/E2
   at zero LUTRAM cost, both families.
2. **Pairing works by pure inference**: `tdp_pair` = exactly ONE RAMB tile
   carrying TWO delay lines. No XPM, no primitives — decision 7.12 holds.
   The collision-free variant `tdp_pair_off` (constant pointer offset
   between the two lines, see below) also infers as exactly one tile —
   the offset costs nothing.
3. **Small depths stay block when hinted** (64x32 → RAMB18): the
   `ram_style="block"` hint overrides the LUTRAM crossover. Consequence for
   `mem_policy`: the hint must be applied *conditionally* by the generator —
   small stage lines should get `distributed` (or no attribute), only
   deeper ones get `block`.
4. **`ram_style="ultra"` alone is sufficient to map onto URAM** — even the
   same-address codings land in URAM288 (`rw1_ultra`, `tdp_pair_ultra` →
   2 blocks at 8192x72). Without the hint the same codings go to BRAM, so
   the earlier claim "same-address never becomes URAM" was a hint artifact,
   not an architectural limit.
5. **Semantics, not collisions, are the real constraint.** Per UG573
   (UltraScale Architecture Memory Resources, "Block RAM and UltraRAM
   Differences"): UltraRAM supports **read OR write per port per cycle**, is
   a superset of SDP but **not TDP**, has **no user-definable
   read-first/write-first/no-change modes**, and "address collision is not
   possible" -- the double-pumped single-port structure accesses port A in
   the first half-cycle and port B in the second, in that fixed order.
   Synthesis maps read-old/write-new codings accordingly (read -> port A,
   write -> port B).

   Because there is no mode knob, whatever the fixed pumping order delivers
   IS the memory's semantics. The golden-model contract pins exact read-old
   values, so the design must not depend on port assignment or pumping
   order at all -- hence the **collision rule** (PLAN.md 2.7): keep read and
   write addresses structurally disjoint.

   - paired BRAM lines: constant nonzero pointer offset between lines
     (`ptr_y` resets to delta != 0) -- cross-port addresses can never meet;
     proven 1 tile (`tdp_pair_off`, both families).
   - URAM-backed lines: split-pointer SDP shape, one line per memory,
     read address structurally != write address (memory inflated to 2D so
     power-of-two lags never alias to zero); proven 2x URAM288
     (`sdp_deep`). Pairing in one URAM is impossible anyway (no TDP mode).
     Further UG573 constraints honored by construction: single clock only
     (matches this core), static cascade only, fixed 4Kx72 width with
     byte-write enables handled outside the array.

   The ultra-hinted-but-same-address codings remain in the spike only as
   documented reference points; the generator will not emit them.

### Caveats

- Synthesis-only spike: functional equivalence of the codings was not
  simulated here (they are standard templates; the FFT core's own L2 suite
  will exercise the real memories end to end).
- Single geometry family per point; the full cutoff sweep (N × R × styles
  with timing) remains P5 work as planned.

---

## S2 — KU3P timing estimate at 500 MHz target (spike, P2)

**Question.** How bad is timing for the naive (correctness-first) RTL at the
500 MHz UltraScale+ target?

**Method.** OOC `synth_design` on `xcku3p-ffva676-1-e`, `create_clock 2.0 ns`,
N ∈ {64, 256, 1024}, 16-bit samples, 18-bit twiddles, auto scaling.
Sources: `spikes/S2_timing/`.

### Results

| N | WNS @ 2.0 ns | CLB LUTs | Registers | DSP48E2 | LUTRAM | BRAM/URAM |
|---|---|---|---|---|---|---|
| 64   | −5.60 ns | — | — | 72  | 176 | 0 |
| 256  | −5.67 ns | — | — | 96  | 290 | 0 |
| 1024 | −5.87 ns | 2520 | 522 | 120 | 746 | 0 |

**WNS is N-independent** (~−5.6..−5.9 ns): the limiter is the per-stage
datapath, not the delay-line depth. Critical path ~7.8 ns ≈ **~128 MHz
achievable** in this naive state.

### Critical path anatomy (N=64 shown; identical shape all N)

```
3.39  (flop start, hidden)
3.92  LUTRAM async read  stages[0].u_stage/mem_re_reg
4.54  CARRY8 chain       wide subtract (older - newer)
5.78  DSP MULTIPLIER     __5 (17x18)
6.69  DSP ALU            __5
7.55  DSP ALU            __6
8.84  DSP ALU            p_1_out__0     <- cascaded P->C
10.12 DSP ALU            p_1_out__1     <- cascaded P->C
```

Two dominant contributors, in order:

1. **4-deep DSP ALU cascade** (~5.6 ns): Vivado serializes the fused
   expression `(d-a)*t_re - (di)*t_im` (and the im twin) through DSP
   accumulators (P→C chaining) instead of parallel multipliers + fabric
   adder tree. The Karatsuba 3-product form (PLAN.md 2.6) with a registered
   P output would cut this to one registered DSP stage + one fabric add.
2. **Async LUTRAM delay-line read** (~4.5 ns incl. subtract): the deep
   distributed-RAM read feeds the DSP A input combinationally. The
   BRAM-backed delay line with registered read (PLAN.md 2.7 output-register
   policy) plus the read-address-ahead trick removes this from the path.

### Resource notes

- 72–120 DSPs = **6 DSPs per stage** for the 4-product complex multiply (the
  Karatsuba form should be 3). At N=1024: 120 DSPs, 2520 LUTs, 522 FFs —
  tiny part usage, as expected for an SDF pipeline.
- All memories are distributed (0 BRAM): stage lines ≤ 512 deep and the
  shared twiddle ROM. LUTRAM is fine for resources but bad for timing at
  depth → BRAM policy (mem_policy) is a timing issue as much as an area one.
- Only 522 registers total: the datapath is fully combinational between
  stage registers. Pipelining (DSP native regs, registered ROM read,
  Karatsuba restructure) is the path to 500 MHz.

### Actions this feeds

- P5 (and the next RTL pass): restructure the complex multiply as explicit
  Karatsuba 3-DSP with native pipeline registers; delay lines to BRAM with
  registered read when `mem_policy` says so; per-stage twiddle ROMs.
  Expect ~2x clock from the DSP restructure alone; measured, not assumed.
