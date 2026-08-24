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
| tdp_pair | 8192x72 | 0 | 16 | 0 |
| sdp_deep | 8192x72 | 0 | 0 | **2** |

Raw reports: `spikes/S1_bram_inference/build/<coding>_<geom>_<part>/util.txt`.

### Findings

1. **(b) infers cleanly**: single-port read-old/write-new maps to RAMB36/E2
   at zero LUTRAM cost, both families.
2. **Pairing works by pure inference**: `tdp_pair` = exactly ONE RAMB tile
   carrying TWO delay lines. No XPM, no primitives — decision 7.12 holds.
3. **Small depths stay block when hinted** (64x32 → RAMB18): the
   `ram_style="block"` hint overrides the LUTRAM crossover. Consequence for
   `mem_policy`: the hint must be applied *conditionally* by the generator —
   small stage lines should get `distributed` (or no attribute), only
   deeper ones get `block`.
4. **Same-address R/W never becomes URAM** (8192x72 → 16×BRAM). URAM
   requires the split-pointer SDP shape: `sdp_deep` → 2×URAM288.
   Architectural consequence: deep delay lines have two clean inference
   variants — same-address (pairs into BRAM, one pointer) vs split-pointer
   (unlocks URAM, costs the second port so no pairing). `mem_policy` can
   therefore offer: mid depth → paired BRAM; very large N → split-pointer
   URAM; both without leaving inference-only RTL.

### Caveats

- Synthesis-only spike: functional equivalence of the codings was not
  simulated here (they are standard templates; the FFT core's own L2 suite
  will exercise the real memories end to end).
- Single geometry family per point; the full cutoff sweep (N × R × styles
  with timing) remains P5 work as planned.
