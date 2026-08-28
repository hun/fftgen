# fftgen — Plan & Architecture (pre-implementation review draft)

A Python generator that emits a finished, parameterized FFT/IFFT Verilog core.
The overall shape follows `~/Projects/firgen_folding`: a pure-Python config +
bit-exact golden model pins down the numerical contract **before any RTL
exists**, then the generated core is verified bit-exactly against that model
under multiple simulators.

---

## 1. Goals & scope

One generator produces a complete, self-contained deliverable set per
configuration:

| Deliverable | Content |
|---|---|
| `fft_core.v` | the streaming core (architecture chosen by parameters) |
| `fft_top.v` | thin parameter-binding wrapper |
| `fft_twiddle_rom_*.mem` | quantized twiddle ROM(s), two's-complement hex |
| `fft_params.vh` | concrete parameters as a Verilog header |
| `params.txt`, `twiddle_map.txt` | human-readable parameter + twiddle quantization contract (ordering, Q-format, symmetry exploitation, C helper) |
| `tb/` artifacts | stimulus + expected-output vectors for bit-exact simulation |

Generator parameters (first version):

| Parameter | Meaning |
|---|---|
| `num_points` | N, power of two |
| `ssr` | R = samples per clock (power of two, `1 <= R <= min(N, 8)` initially) |
| `forward` | FFT / IFFT (`inverse=False/True`) |
| `sample_width` | signed **input** width (I/Q, applied to both components) |
| `output_width` | signed **output** width (separate from input width) |
| `sample_decimal` / `output_decimal` | fractional bits (output auto-derived if omitted) |
| `twiddle_width` / `twiddle_decimal` | signed twiddle fixed-point format |
| `input_order` | `native` or `bitreversed` |
| `output_order` | `native` or `bitreversed` |
| `scaling` | per-stage shift schedule: `auto` (default, **conservative — provably cannot overflow**) or explicit list of 0/1/2-bit shifts per stage (aggressive, for exotic use cases) |
| `rounding` | fixed policy: round-half-up at scaling shifts, truncate at products |
| `dsp_preset` | target family → `(A_width, B_width)` pair (see §2.6); configs exceeding the hard-multiplier ports are **warned about, not rejected** (decision §7.13) |
| `twiddle_mem_style` | `auto` / `distributed` / `block` / `ultra` (§2.7; `auto` uses the empirically derived cutoff table) |
| `delay_mem_style` | same choice for the per-stage feedback delay lines |

**Width doctrine (deliberate asymmetry):** *samples are wide, twiddles are
narrow* — mirroring the DSP48 hard macro. Typical operating point:
`sample_width` up to the DSP A-port maximum (25 on DSP48E1, 27 on DSP48E2,
18 on 18×18 fabrics) while `twiddle_width ≤ 18` rides the B port. The
generator treats this as the default and warns (or errors, per preset policy)
when a configuration cannot map onto the hard multipliers.

Interface: **AXI4-Stream-encoded data, but no handshake** — the port set
follows AMBA AXI4-Stream signal naming and packing (`tdata` with byte-aligned
I/Q lanes like firgen: `lane i at [i*PITCH +: W]`, `tvalid`, `tuser(0)` =
start-of-frame, `tlast` = end-of-frame, on both slave and master sides)
**minus `tready`**: no backpressure anywhere inside the core. Frame sidebands
ride at fixed latency (decision §7.2 — they exist for off-by-one detection,
not flow control). One frame = N complex samples; steady-state
throughput is **one frame per N/R clocks** while `ce=1`, back-to-back
capable. Flow control is deliberately *outside* the core: if the surrounding
system needs backpressure, it buffers (a full frame in, and/or out) in FIFOs
in front of/behind the core and gates `ce`. This keeps the datapath free of
valid/ready plumbing entirely.

Non-goals for v1: arbitrary (non-power-of-2) N, fractional L/M resampling,
block floating point (listed as future work), runtime-twiddle reload.

## 2. Architecture

### 2.1 Baseline core (R = 1): fully pipelined single-path delay feedback (SDF), DIF

**Decision:** a *fully pipelined streaming* core — not a buffered/macro
engine that ingests a frame, computes, and emits. The chosen topology is the
classic DIF-SDF pipeline (Takala/Baas style), **implemented as plain
radix-2** (the radix-2² folding is deferred — Appendix A), selected over the
alternatives on resource grounds:

| Topology | Multipliers | Memory | Streaming? |
|---|---|---|---|
| **SDF (chosen)** | ≈ N complex (plain radix-2; ~N/4 with the deferred R2² folding) | ~N delay | yes, continuous |
| MDC / R2²SDF variants | same order | ~2–3× more (commutators) | yes |
| Buffered/burst (in-place array) | few, time-multiplexed | full frame RAM ×2 + controller | no — dead cycles between frames |
| Fully parallel | O(N log N) | minimal | yes but absurd area for large N |

SDF is the minimum-delay-memory continuous-stream design (one delay line
per stage, one multiply per butterfly); MDC only wins when R > 1 needs
multiple streams anyway (§2.4 covers that case by composing SDF engines).

**Streaming contract** (pinned, tested): one complex sample accepted per
clock, forever — frames may be fed back-to-back with no gap; outputs emerge
continuously at the same rate after a fixed latency of `L` cycles (exact
deterministic constant derived from the stage/reorder pipeline and reported
in `params.txt`). No start pulse, no inter-frame idle, throughput = 100 % of
clock rate at R=1. Deasserting `ce` freezes the whole datapath — values never
change under stall; no sample is lost or altered.

- **Decimation-in-frequency**: natural-order input → bit-reversed-order output
  *by construction*. This makes the "native output" case an explicit, testable
  reorder buffer instead of hidden shuffling inside the datapath.
- `log2(N)` pipeline stages, **plain radix-2 as implemented**: every pair
  multiplies (uniform datapath, Appendix A), delay depth `D_s = N/2^(s+1)`
  (DIF) or `2^s` (DIT). P6 exception: the DIF LAST TWO stages (DIT first
  two) multiply only by single-component twiddles (W^0 real, W^{N/4} = ±j)
  and emit the product as exact fabric shift/subtract logic — no DSPs
  (§5 P6). The radix-2² folding that would cut multiplies to
  ~N/4 is deferred: it changes *which cycles* carry multipliers and must be
  re-proven numerically equivalent to the pinned per-stage shift schedule
  before any RTL may use it (Appendix A).
- Per-stage feedback delay line of depth `D_s`, realized per the decided
  memory cutoffs (LUTRAM ≤ 1 kbit, BRAM < 256 kbit, URAM above —
  `doc/mem_cutoffs.md`), always with synchronous read lagging the write.
- Twiddles: the canonical table is quarter-wave compressed (N/4 + 1
  entries, decision §7.6); the generated `.mem` expands it into per-stage
  slices (one N-word file, stage `s` reads `[BASE_s .. BASE_s + D_s − 1]`).
  Exact layout and quantization contract: `twiddle_map.txt`.
- Pipelining mirrors firgen discipline: fabric registers around the ROM /
  multiplier paths so DSP blocks infer AREG/BREG/MREG/PREG; timing closure is
  a design requirement, not an afterthought.

Throughput: 1 sample/clock, latency ≈ N + pipeline + reorder depth (exact
constant reported by the generator and asserted in tests).

### 2.2 IFFT

IFFT = conjugate twiddles + a 1/N scaling. The 1/N is **folded into the
per-stage scaling schedule** (distributed right shifts), so the inverse core
is the same datapath with conjugated ROM contents and a different shift
schedule — one RTL module, two generated configurations. Verified separately.

### 2.3 Input/output ordering

The DIF/DIT duality eliminates the reorder buffer for half the ordering
space. The generator emits the matching **topology**, not a shuffling
wrapper:

| `input_order` | `output_order` | Generated topology | Reorder RAM |
|---|---|---|---|
| native | bit-reversed | **DIF-SDF** (its natural output order) | none |
| bit-reversed | native | **DIT-SDF** (mirror datapath, identical cost) | none |
| native | native | DIF + output reorder, or input reorder + DIT | 1× N |
| bit-reversed | bit-reversed | same two options | 1× N |

Notes:

- Feeding bit-reversed input into the *DIF* datapath is **not** an option:
  `F·R ≠ R·F` (checked numerically at N=4) — the result would be neither
  natural nor bit-reversed ordered. The DIT variant is the only free path.
- DIT-SDF resource profile equals DIF-SDF: same butterfly/multiplier count,
  same twiddle ROM sizes, same delay-line depths (mirrored along the
  pipeline). All verification layers treat topology as just another config
  dimension.
- The canonical zero-hardware chain is DIF-FFT (TX, native in / bitrev out)
  paired with DIT-IFFT (RX: conjugated twiddles, 1/N in the shift schedule,
  bitrev in / native out).
- Where a reorder buffer is unavoidable (native→native etc.), it is the small
  `fft_reorder.v` module: an N-deep ping-pong RAM that also provides the
  frame-boundary marker (`tuser`-style framing, open question §7) and makes
  back-to-back frames trivially correct.

### 2.4 SSR (R > 1 samples per clock)

Cooley-Tukey decomposition `N = R · M` (with `M = N/R`):

1. Each of the R input lanes feeds its own **M-point sub-FFT** — internally
   the *same* R=1 SDF engine as §2.1, instantiated R times.
2. A cross-lane stage applies the twiddles `W_N^{r·k}` and performs the
   R-point combining across lanes (all R lanes available in one clock, so the
   R-point step is a small fully-parallel butterfly network).
3. Lane permutation / digit-reversal effects land in the reorder logic.

This keeps the SSR core a composition of the verified R=1 engine plus a small
crossbar — far easier to verify than a monolithic MDC. The golden model
mirrors this composition exactly. Constraint `R | N`, both powers of two.

**P4 CLOSED — integration verified, timing met (N=8 R=2, KU5P):**

* **RTL vs golden bit-exact** at R=2 (all four order corners via the
  lane engines; suite 92 tests green, no skips).
* **Crossbar pipeline (6 stages)**: `fetch(wq/d) → products(pp, one DSP
  each) → combine(b = pp1−pp4 + j·(pp2+pp3)) → lane-DFT(h) → s_x half
  shift(x) → rescale+sat(dout)`. `CB_LAT = 6` in RTL *and* golden;
  emission gate taps `pd6`.
* **KU5P post-route @ 2.0 ns**: WNS **+0.075 ns**, TNS 0, all nets
  routed; 20 DSPs, 1063 CLB LUTs, 2139 FFs (<0.5% each).
  Checkpoint `build/ssr_synth/route.dcp`.

**R=4 and R=8 closed (N=16, KU5P @ 2.0 ns):**

| config | WNS post-route | DSPs | LUTs | FFs |
|---|---|---|---|---|
| R=2 N=8 | +0.075 | 24 | 1063 | 2139 |
| R=4 N=16 | +0.021 | 44 | 2309 | 3913 |
| R=8 N=16 | +0.037 | 64 | 3291 | 5056 |

* R=4: 4x M=4 lane engines + 4-lane crossbar; INVERSE-aware second
  layer (the +/-j signs conjugate; R=2 never exposed this).
* R=8: 8-point lane DFT via G/H split -> alternating inner-pair
  (+/-j, conjugates under inverse) -> sigma-signed Q -> ONE real
  scalar multiply by Q(td) sqrt(2)/2 per odd-q bin. Golden mirrors
  the E_q + round_shift(F_q*c, td) structure exactly.
* The 39-bit x 18-bit odd-path multiply does NOT auto-infer DSPs
  (fabric, 7 CARRY8s): split U = U_hi*2^18 + U_lo (signed/unsigned)
  so each partial maps to ONE DSP48E2, combine in a registered stage.
* R=8 crossbar is 10 stages (CB_LAT=10, pd10): fetch, pp, b,
  G/H(h1), Q/P+e(h2a), sigma-U(h2b), split products(t3a),
  combine+pe+ce(t3b), s_x shift(x), rescale(dout).
* I/O pin note: ffva676 has only 256 user I/O; the SSR top with R*16
  axis buses exceeds it (264 ports at R=4, 519 at R=8), so timing
  runs use the ssr_timing_wrap timing vehicle (internal LFSR stimulus
  + checksum sink; only {clk,rst,done} on the boundary) on the real
  target part. Same die => identical internal critical paths.
* **Wide-N R=1 sweep (N up to 16384) — CLOSED:**
  * Two preload-width fixes (hardcoded 512b pack macro; stage
    `PRELOAD_I` parameter was [7:0]) brought N=1024..8192 green.
  * The N=16384 "residual" (and SSR R=2 N=1024) shared ONE real root
    cause: the butterfly pre-adder path was WIDTH bits wide, but two
    full-scale stage outputs can sum/differ to WIDTH+1 bits before
    the per-stage >>SHIFT. Product-path outputs legitimately reach
    +-65k (multiply runs on the butterfly diff), so a rare collision
    of two large same-sign values wrapped mod 2^WIDTH; the single
    bit-16 error then doubled through each downstream butterfly
    (~N/4 bad words, tail-quarter concentration). Fixed by carrying
    pre-adder -> ADREG -> cascade -> combine at BW = WIDTH+1 (18b =
    DSP48E2 B-port native); see commit "widen butterfly pre-adder".
    R=1 now bit-exact through N=16384. The earlier "flag-entry
    timing" theory was a probe-alignment artifact.
**R=1 KU5P OOC synth sweep @2ns (post-synth estimates):**
  * DSPs = 4 x num_stages exactly (512->36 ... 8192->52); BRAM/URAM 0.
  * LUTRAM grows ~linearly with N: 1.8k (512), 3.1k (1024), 5.8k
    (2048), 11.1k (4096), 21.6k LUT (8192) -> P5 memory policy needed
    before N>=16384 is practical on KU5P.
  * WNS -0.020 ns constant across all N (critical path is intra-DSP:
    A/B-reg -> preadd -> mult -> ALU on the cascade hop, independent
    of transform size). Same path closed post-route at +0.090 on the
    N=64 PR vehicle; a mid-size PR confirmation run is cheap insurance.
**BRAM->DSP pipelining (NLAYERS 7->10) — CLOSED (this is the layer
    increase the DSP-reduction experiment parked):**
  * Golden `_SDFStage` rewritten to NLAYERS=10 mirroring the RTL register
    chain exactly: L0 BRAM output reg + input + twiddle; L1/L2 two DSP
    input registers (AREG/DREG/BREG); L3 butterfly (pre-adder -> ADREG);
    L4 im-path products (MREG) + freeze of re operands; L5 C-port regs
    (CREG) + re-path products; L6 post-adder (ALU P -/+ C -> PREG); L7
    fabric combine; L8 shift staging; L9 out + product-FIFO write.
    The BRAM read-address register (raddr) is modeled explicitly (always
    wptr - D; adds no data latency, matches the RTL's registered address).
  * The C-port pairing requires the im-path products one cycle ahead of
    the re-path (im MREG routes to the re DSP's C port, CREG at L5), so
    the re operands are frozen at L4 into hold registers. Validated
    bit-exact vs batch for N=8..16384, DIF+DIT, fwd+inv.
  * Preload pack grew 71->74 bits/stage (pipe 6->9 bits); the phase gates
    follow the validated layer-k = pipe_comp[k-1] pattern.
  * DSP inference (N=64 KU5P OOC, dsp_audit): the im-path product DSPs
    absorb AREG=2 BREG=2 DREG=1 ADREG=1 MREG=1 CREG=1 PREG=1 -- the full
    register budget; the re-path combine DSPs use AREG=1/BREG=1 plus the
    C-port. The butterfly diff is computed in the DSP pre-adder (no
    fabric carries on the multiply path). Making the butterfly UNGATED
    (PASS values unused downstream) was the key that let Vivado map
    D-A into PREADD instead of fabric CARRY8s.
  * R=1 KU5P OOC @2ns: all sizes MET -- 64: +0.700, 1024: +0.605,
    2048: +0.563, 4096: +0.184, 8192: +0.163. Critical path is now the
    product-FIFO LUTRAM read (wptr -> pfifo -> out), not the DSP.
  * SSR R=2 N=8192 @2ns: WNS -0.144, critical path is the CROSSBAR
    input (reorder BRAM -> crossbar DSP B input), not the stage
    pipeline.
**Crossbar input staging — CLOSED (R=2 N=8192):**
  * Root cause: the lane reorder buffers are depth-cascaded RAMB36E2
    (~1.25 ns clock-to-out on CASDOUT); the crossbar's single input
    register stage (d_re/d_im) was absorbed into the product DSPs'
    A/B ports, so the BRAM clock-to-out shared a cycle with the DSP
    input setup (plus ~0.45 ns clock skew at synth estimates).
  * Fix: a dedicated fabric input register (q_re/q_im) at the crossbar
    boundary, with the twiddle riding the same +1 delay (wa -> wq) so
    the (word p, W^{r*p}) pair still meets at the multiply. Two
    registers ahead of the multiply leave the first in fabric: the
    BRAM->FF and FF->DSP hops are both short. CB_LAT 6->7 (R<8) and
    10->11 (R>=8); the frame-sync pd tap moved to pd7/pd11;
    golden_ssr.CB_LAT updated to match.
  * Result: post-synth WNS -0.144 -> -0.020; the critical path moved to
    the intra-DSP cascade hop (pp A/B-reg -> preadd -> mult -> ALU ->
    PREG, MREG=0 sender half of the PCIN cascade into b_*), the same
    ~1.85 ns path documented in S3.
  * Post-route (AggressiveExplore double-pass): WNS -0.021, TNS
    -0.152, 16 failing endpoints -- effectively closed (skew-dominated
    synth estimate; accepted as closable in an integrated design).
  * Open items: the pfifo read register is muxed (sum_out vs DOB) so the
    deep pfifos stay LUTRAM; a dedicated DOB_REG restructure was tried
    and reverted (subtle read-window off-by-one; redo carefully with a
    golden-side pfifo_dout layer if the pfifo read becomes binding).

**DSP reduction experiment (3-mult Gauss complex product) — REJECTED
for now, evidence kept:**
  * Rewrote fft_stage's complex multiply as k1=t_re*(d_re+d_im),
    k2=d_re*(t_im-t_re), k3=d_im*(t_re+t_im); re=k1-k3, im=k1+k2.
    Integer-exact vs golden; full suite + wide ladder green.
    DSPs drop 4->3 per stage (52->39 at N=8192).
  * BUT synthesis shows the DSP48E2 pre-adder sits IN SERIES with the
    multiplier (PREADD->MULT->ALU): intra-DSP path grows 1.85 -> 2.61 ns,
    WNS -0.78 at 2 ns. Fabric pre-adds are worse (the original Karatsuba
    lesson). Closing it requires +1 pipeline layer on the product path
    (NLAYERS 7->8: register pre-add results, multiply next cycle), which
    changes the golden contract, preload pack layout and every bit-exact
    baseline. Parked unless DSP area becomes the binding constraint.
  * Implementation traps worth remembering if revived: (1) replicate-
    concat "sign extension" with a NEGATIVE count silently truncates --
    PAW must be max(BW,TWIDDLE_WIDTH)+1, not BW+1; (2) extending BOTH
    multiplier operands to PAW puts a >18b value on the DSP48E2 B port
    and splits one multiply into two DSPs -- keep the raw-width operand
    natural so it lands on B; (3) plain signed add wires get context
    widening for free -- hand-rolled extensions just add fabric glue
    that blocks AREG/DREG absorption.

**Debug lessons** (see also §"Convention traps"):
  - a Python cycle-exact replica of the RTL pipeline
    (`spikes/xbar_sim.py`) fed with golden lane streams finds
    divergences in seconds and validates fixes before re-running
    Verilator/Vivado;
  - registers declared but never assigned silently read as zero in
    Verilator (`pd4/pd5` gate taps) — Vivado lint is the only netlist-
    level check that flags such things as constant-driven logic;
  - multi-driven nets (same reg reset in one always block, driven in
    another) fold the whole datapath to constants in Vivado with only
    CRITICAL warnings — always grep the synth log.

**P4 status & refined contract** (implemented, bit-exact for R = 2 and 4;
R = 8 golden-verified):

* **Structure**: lane r receives `x[R·j + r]` (stride-R demux is free
  wiring). Each lane is a full M-point `fft_top` with `REORDER_OUT = 1`,
  so lanes emit `A_r[p]` in **native p order**, lockstep-aligned.
* **Crossbar** (`fft_cross.v`): pre-twiddle `B_r = A_r · W_N^{r·p}` — the
  ROM holds **R rows including r = 0** (`W^0` in Q(td)); skipping row 0
  makes that lane's contribution 2^td times smaller and the butterfly
  sums cancel it (this bug produced exact ±pairs on the output). The
  lane-DFT applies `W_R^{rq}`: for R ≤ 4 these are exactly
  `{0, ±1, ±j}` → add/sub/swap only; R ≥ 8 needs constant-multiplier
  layers (W_8 parts involve √2/2; golden path implemented via Q(td)
  coefficients, frac becomes od + 2·td).
* **Output order contract**: emission is frame-synced (drop until the
  first mature p == 0 word, then continuously valid) and each output
  word carries `X[qM + p]` on lane q — over a frame, output lane q holds
  the **contiguous block X[qM .. qM+M−1]** ("block-contiguous" order).
  Truly consecutive per-word packing would need an M×R corner-turn
  buffer; deferred (downstream can also absorb it).
* **Markers**: SOF enters with sample n = 0 (lane 0's first sample) and
  emerges on output lane 0 at p = 0; EOF enters with n = N−1 (lane R−1)
  and emerges on lane R−1 at p = M−1. Marker pipeline depth equals the
  crossbar latency.
* **Scaling**: crossbar adds one rounding shift s_x = log2(R); combined
  with the lanes' log2(M) this totals log2(N), matching §2.5. Lane
  outputs re-quantize at the crossbar boundary → vs monolithic batch the
  reference tolerance is R/2 + 1 LSB (documented double quantization).

### 2.5 Fixed-point contract

Quantization points (must match RTL register-for-register, like firgen):

| Stage | Operation | Width |
|---|---|---|
| twiddle ROM | Python-side quantization, exported `.mem` | `twiddle_width` |
| complex multiply | product truncated/rounded to a defined intermediate width | documented per config |
| per-stage scaling | right shift `shift[s]` (0–2 bits) with chosen rounding | accumulator width |
| output | final quantization to `output_width` (round + saturate) | `output_width` |

`scaling="auto"` computes a **conservative** shift schedule (decision §7.3):
a worst-case full-scale sine cannot overflow *by construction* — the bound
is proven over the schedule, not simulated — with shifts distributed as late
as possible to minimize precision loss. Explicit schedules remain available
for aggressive/overflow-tolerant use cases. The resulting schedule is
printed into `params.txt` and pinned by golden-model tests; the generator
reports expected SQNR (from the shift schedule + twiddle quantization)
alongside the design.

All quantization helpers live in one module (`src/quant.py`), shared by
golden model and any analysis tools — a single canonical definition, as in
firgen.

### 2.6 DSP mapping — asymmetric ports, pre-adder, complex multiply

The complex butterfly multiply is planned **onto the DSP48 shape**, not onto
an abstract `*` operator:

**Target scope:** **Xilinx only for v1** (7-series DSP48E1, UltraScale+/
Versal DSP48E2). Portability through pure inference stays a stated long-term
goal — so the *numerical* contract (widths, schedule, golden model) is
written family-agnostically in Python — but RTL is allowed to use
Xilinx-specific attributes and instantiations wherever inference is fragile
or leaves performance on the table (URAM, DSP pipeline attributes,
`ram_style`/`uram_style`). Nothing in the plan depends on that escape hatch;
it's a permitted tool, not a requirement.

| Family              | Hard mult | A / pre-adder | B  | Accumulator | Notes                |
|---------------------|-----------|---------------|----|-------------|----------------------|
| 7-series DSP48E1    | 25×18     | 25 (+pre-add) | 18 | 48          | Artix/Kintex         |
| UltraScale+ DSP48E2 | 27×18     | 27 (+pre-add) | 18 | 48          |                      |
| Versal DSP58        | 27×24     | 27 (+pre-add) | 24 | 48          |                      |
| (other fabrics)     | —         | deferred      | —  | —           | post-v1, via presets |

Consequences baked into the datapath:

- **Sample data occupies the A/pre-adder port** (`sample_width ≤ 25/27`, one
  bit less when a fabric pre-adder feeds A); **twiddles occupy B**
  (`twiddle_width ≤ 18`). This is exactly the wide-sample/narrow-coefficient
  split requested — the generator computes and reports the exact fit per
  preset.
- Product width `sample_width + twiddle_width` always fits the 48-bit
  post-multiplier accumulator with headroom, so **products are never
  quantized internally** — the only quantization points remain the per-stage
  scaling shifts and the final output (simplifies both RTL and golden model).
- **Complex multiply = 4 DSPs** (4-product split with DSP C-port chaining,
  *implemented; supersedes the earlier 3-DSP Karatsuba decision*): the
  im-path products `diff·t_re` / `diff·t_im` run one cycle ahead (L4), their
  MREGs route to the re-path DSPs' C ports (CREG, L5), and the ALU forms
  `re = P − C` / `im = P + C` (L6) — 4 DSPs per stage, exactly 4·log2(N)
  per R=1 core (measured: datasheet sweep). The 3-multiplier Karatsuba/Gauss
  forms were measured and **rejected on timing**: on the DSP48E2 the
  pre-adder sits in series with the multiplier, so the extra pre-add term
  grew the intra-DSP cascade hop 1.85 → 2.61 ns (WNS −0.78 @ 2 ns) — it
  misses the 500 MHz goal (evidence kept in the P5a notes of §2.4).
- Pipeline registers follow the hard macro: A/B/AREG/BREG/MREG/PREG, matching
  firgen's proven floorplaining-free closure approach.
- DSP count formula (reported by the generator, asserted by a resource test):
  `4 · num_stages` — audited netlist-level: the im-path product DSPs absorb
  the full DSP48E2 register budget (AREG=2 BREG=2 DREG=1 ADREG=1 MREG=1
  CREG=1 PREG=1); the re-path DSPs add AREG/BREG + C port (P5a dsp_audit).

### 2.7 Memory hierarchy — twiddle ROMs and delay lines

Storage style is an explicit generator decision with three levels and
**empirically derived cutoffs** (not guessed constants):

| Level | Technology | Sweet spot |
|---|---|---|
| L0 | FF/SRL shift registers, constant-address ROM in LUTs | tiny depths, parallel-read structures |
| L1 | Distributed RAM (LUTRAM) | small tables/lines, dual-port for free |
| L2 | Block RAM (RAMB18/36) | mid/large depths; synchronous read costs +1 pipeline stage |
| L3 | UltraRAM (URAM) | very deep lines (huge N, SSR lane buffering) |

**Hard rule: URAM never stores twiddles.** URAM has no power-up
initialization, so it is unusable as ROM — the twiddle tables are confined to
registers/LUTRAM/BRAM (all of which support `INIT`). Conversely, URAM is
*only* attractive for the delay lines, where it excels.

**Port model** — both BRAM and URAM expose two native, independent ports;
the generator assigns them explicitly instead of leaving it to inference:

| Consumer | Port A | Port B |
|---|---|---|
| Twiddle ROM (BRAM) | read addr → data (+1 sync stage) | free — available for a paired ROM or a neighboring delay line |
| Delay line, small/mid (BRAM) | read oldest @ rd-ptr | write newest @ wr-ptr |
| Delay line, large (URAM) | write newest @ wr-ptr | read oldest @ rd-ptr |
| SSR lane/corner-turn buffer | lane-group write | lane-group read |

Consequences:

- **One delay line consumes exactly one port pair** (1R + 1W per clock).
  Two shallow delay lines therefore pack into one true-dual-port BRAM (one
  line per port pair is impossible — instead: line X on port A R/W via
  read-old/write-new same-address trick frees port B for line Y). The
  generator applies a pairing heuristic over the geometric depth ladder
  (pair adjacent stages whose depths land in the same BRAM size class) and
  reports the resulting block count — a direct resource win the sweep will
  quantify against the unpaired baseline.
- **URAM width packing**: the native 72-bit word fits complex samples with
  room to spare (e.g. 2×27+2×18, or four half-word lanes). For SSR, several
  lanes' delay lines can be bit-sliced into one URAM word so one cascade
  serves multiple lanes; depth extends through the dedicated cascade path
  (deep lines without controller overhead). Only worthwhile where the sweep
  shows BRAM counts dominating the part.
- **Initialization invariance (a provable property, not luck)**: any RAM
  style — including uninitialized URAM — yields bit-identical results,
  because every read address lags its write address by strictly less than
  the number of clocks since reset; stale power-up contents are always
  overwritten before they could ever be read. This holds per delay line
  individually and makes `mem_style` a pure implementation choice. It is
  pinned by a dedicated test: identical stimulus under all styles, plus a
  randomized-initial-memory simulation mode for the RTL.
- **Reset semantics**: pointers clear on reset; in-flight contents are
  discarded, and the fixed-latency contract restarts (same drain-and-refill
  behaviour the golden model already mirrors).

**Collision rule (hard requirement).** Per UG573 ("Block RAM and UltraRAM
Differences"): URAM supports read OR write per port per cycle, is a superset
of SDP but not TDP, has NO user-definable read-first/write-first/no-change
modes, and address collision is not possible -- it is double-pumped
single-port memory: port A accesses in the first half-cycle, port B in the
second, fixed order. Consequences:

- A same-cycle read + write pair MUST split across the two ports; the fixed
  A-first order forces read -> port A, write -> port B for read-old
  semantics. This is what synthesis emits when mapping read-old/write-new
  codings onto URAM.
- A URAM-backed delay line consumes BOTH ports of its memory: pairing two
  lines in one URAM is structurally impossible (unlike BRAM).
- Because there is no semantics knob, whatever the pumping order delivers IS
  the behavior. The golden-model contract pins exact read-old values, so the
  RTL must not depend on port assignment or pumping order at all: keep read
  and write addresses structurally disjoint.

Design shapes (both proven by spike S1, see `doc/mem_cutoffs.md`):

- **Paired BRAM/LUTRAM lines**: two lines share one true-dual-port array,
  one line per port (per-port read-old/write-new is fine on BRAM). A
  constant nonzero reset offset between the pointers (`ptr_y` resets to δ≠0)
  keeps cross-port addresses apart forever — zero hardware cost
  (`tdp_pair_off`: exactly 1 tile, both families).
- **URAM-backed lines**: split-pointer SDP shape, ONE line per memory:
  port B reads at `rptr = wptr − D (mod M)` with memory size `M = 2·D`, so
  power-of-two lags can never alias to zero (`sdp_deep`: 2× URAM288 at
  8192x72). `ram_style="ultra"` alone also maps same-address codings onto
  URAM (S1 reference points `rw1_ultra`/`tdp_pair_ultra`) — correct only
  while the inferred port ordering holds; not emitted by this generator.

**Output-register policy** (applies to every RAM in the design — twiddle
ROMs and delay lines alike):

- **Minimum 1 output register** (synchronous read): this is what Vivado needs
  to infer BRAM/URAM at all, and it is *required for acceptable timing* on
  distributed RAM — an async-read LUTRAM feeding logic directly caps clock
  rate badly on anything but tiny depths. The datapath schedule already
  assumes registered ROM reads (see firgen's fabric-register discipline), so
  stage RTL is written around a fixed read latency of 1.
- **2 output registers are the default**: on BRAM the second stage maps onto
  the dedicated hardware output register (DOA/DOB pipeline stage) — free
  timing, no extra block; on LUTRAM and URAM it's one extra fabric register,
  costing nothing that matters and buying the same timing relief.
- Read latencies per `(consumer, mem_style)` pair are compile-time constants
  emitted into `params.txt` and mirrored by the golden model — consistent
  with the initialization-invariance argument, they change latency only,
  never values (pinned by the L4b equivalence suite).

**Cutoff methodology:** the constants live in one module
(`src/mem_policy.py`) and are produced by measurement, not intuition:

1. Every storage instance carries a `mem_style` override so any config can be
   forced onto any level.
2. The P5 synthesis sweep generates a matrix (N × R × style-forcing), runs
   **Vivado OOC** on the two target families (Artix-7 @250 MHz,
   UltraScale+ @500 MHz — URAM points only meaningful on UltraScale+), and
   records LUTRAM/BRAM/URAM/FF/DSP counts and WNS per point.
3. The crossover points (where BRAM becomes cheaper than LUTRAM in both area
   and timing, where URAM beats BRAM) are transcribed into `mem_policy.py`
   together with the sweep report — `doc/mem_cutoffs.md` documents the table
   and the raw data behind `auto`.

Working hypotheses to validate (initial defaults until the sweep runs):
twiddle ROM ≤ ~128 total words → distributed; delay line depth ≤ ~64 →
SRL/LUTRAM; BRAM from there; URAM entered only where the BRAM count for
deep delay lines would dominate the part (expected around `N ≥ 2^16` on
delay lines, SSR lane buffers possibly earlier). Cutoffs are tracked
separately per consumer class — ROM and delay-line crossover points differ
by design.

### 2.8 Core interface & flow control

**Ports follow AXI4-Stream encoding minus `tready`.** Slave side:
`s_axis_tdata`, `s_axis_tvalid`, `s_axis_tuser(0)` (SOF), `s_axis_tlast`.
Master side: the same signals prefixed `m_axis_`. Plus `clk`, active-high
synchronous `rst` (control state only, §2.9), and `ce`.

Behavioral contract:

- The datapath advances when `ce && s_axis_tvalid`; either low freezes it —
  a single AND gate, no stall logic in any stage; values never change while
  frozen.
- `m_axis_tvalid` marks cycles carrying valid output samples (low during
  reset/fill and whenever the datapath is frozen); `tuser(0)`/`tlast` are
  transported at the fixed latency `L`, uninterpreted by the datapath.
- The reorder buffer consumes input markers and regenerates output markers
  (it owns the frame boundary); the golden model asserts marker alignment
  every frame as an off-by-one guard.

Rationale: with a fixed-latency, fixed-throughput pipeline there is nothing
for an internal handshake to decide — every cycle either advances the stream
or doesn't. Valid/ready negotiation inside the core would buy generality at
the cost of stall/freeze logic in every stage, handshake corner cases in RTL
*and* golden model, and a whole verification dimension (irregular-valid,
stall patterns, combined) that firgen needed hundreds of tests for.

Flow control lives outside:

- A producer that cannot sustain one sample/clock drives `ce` (the core is a
  pure clock-enable freeze: zero bubbles enter the datapath, values hold).
- A consumer that cannot keep up uses an output FIFO (frame-deep for full
  decoupling) and drops `ce` when it fills — the classic elastic-buffer
  pattern, identical to how firgen's testbenches wrap group cores.
- If/when a standards-compliant AXI4-Stream wrapper is wanted, it is a thin
  module *around* the core (`fft_axi_top.v`, future work) — the verified
  numerical datapath never changes.


### 2.9 Reset & register policy

Reset is a routing/timing resource like any other — the policy minimizes its
fanout deliberately:

- **No resets on direct-datapath registers**: RAM output registers, DSP
  input/pipeline/output stages carry *no reset at all*. Their contents before
  the first valid sample are don't-care by construction (same argument as the
  initialization invariance of §2.7 — datapath values are never read before
  they have been written downstream of valid data). This also keeps DSP48
  internal registers free of reset constraints.
- **Control/state registers don't all need it either** (e.g. pure pipeline
  shift registers); those that do — pointers, FSM state, schedule counters —
  use **synchronous reset only**, to a defined value (0 where meaningful). No asynchronous resets anywhere: better
  register packing, no recovery/removal analysis burden, matches Xilinx
  recommended practice for BRAM/URAM/DSP-adjacent fabric.
- Consequence for verification: after reset, outputs preceding the fixed
  latency fill are undefined and excluded from comparison (the golden model
  gates them identically); the mid-run-reset test asserts clean recovery via
  the *control* state only. Testbenches must not assume initialized
  datapath registers — same discipline as the randomized-memory mode of L4b.

## 3. Verification strategy (layered, model-first)

The defining property of the project: **the bit-exact contract exists and is
tested before RTL is written.**

| Layer | What | Against what | Tolerance |
|---|---|---|---|
| L0 | `numpy.fft` float reference + config/schedule unit tests | math | — |
| L1 | Cycle-accurate golden model (`tick()`-style stream API + `process_stream()` convenience) | L0 | SQNR bound (e.g. > 60 dB typical config), no overflow assert |
| L2 | Generated RTL (Verilator **and** Icarus) driven by golden-generated vectors | L1 | **bit-exact**, word-for-word |
| L3 | Protocol/robustness suites | L1 | bit-exact under freeze patterns (`ce` and/or `s_axis_tvalid` low; periodic + pseudo-random), reset mid-run, and — where the optional FIFO wrapper exists — backpressure through the wrapper |
| L4a | Coverage sweep: config matrix (N × R × widths × orders × fwd/inv × scaling × **mem styles**) under Verilator with `verilator_coverage`; formal properties (stability, causality, exact spacing) via yosys SAT — same recipe as firgen's `coverage_formal.md` | — | 100 % line coverage on emitted RTL templates |
| L4b | **Memory-style equivalence**: every config verified bit-exact under *each* forced `mem_style` — switching storage technology may change timing/latency contract only through declared, tested pipeline constants | L1 | bit-exact |
| L4c | **Resource sweep** (P5): Vivado OOC synthesis over the N × R × style matrix on Artix-7 + UltraScale+; establishes the empirical `mem_policy` cutoffs and validates the DSP-count formulas | — | measured table in `doc/mem_cutoffs.md` |

L1→L0 cross-checks catch architectural bugs (wrong twiddle exponents,
transposed stage order) cheaply in Python; L2 catches only RTL bugs, against
an already-trusted oracle.

Simulator artifacts follow the firgen pattern per build directory:
`stimulus.txt` (ce-freeze patterns included), `expected.txt`, `Makefile`
— so every configuration is reproducible from the command line.

## 4. Project layout

```
fftgen/
├── PLAN.md                  ← this file
├── README.md                (written once v1 works)
├── pyproject.toml           pip install -e ., src-layout like firgen
├── doc/
│   ├── architecture.md      (post-implementation write-up)
│   └── coverage_formal.md   (L4 results)
├── src/
│   ├── config.py            FFTConfig + validation (all constraints rejected here)
│   ├── mem_policy.py        storage-style decisions + empirical cutoff table
│   ├── quant.py             canonical fixed-point quantize/round/saturate
│   ├── twiddles.py          twiddle generation, quantization, .mem/.map export
│   ├── golden.py            float reference + cycle-accurate stream golden models
│   │                        (SDFGoldenModel, SSRSDFGoldenModel, Stream wrappers)
│   ├── stimuli.py           impulse, single tones, chirp, random, ce patterns
│   ├── analysis.py          SQNR / overflow / gain metrics vs float reference
│   ├── fft_gen.py           artifact generation + verify_verilator/verify_icarus drivers
│   └── export_core.py       headless CLI export of the full deliverable set
├── rtl/
│   ├── fft_sdf.v            R=1 SDF core, plain radix-2 (all stages, parameterized)
│   ├── fft_cross.v          SSR cross-lane R-point combiner + lane permute
│   ├── fft_reorder.v        native ↔ bit-reversed reorder buffer
│   └── fft_top.v            wrapper assembling sdf×R + cross + reorder
├── tb/                      Verilator (.cpp) + Icarus (.v) testbenches
├── tests/                   unittest suite (L0–L4), skipped when sims absent
└── build/                   generated per-config artifacts (gitignored)
```

RTL is written as hand-authored parameterized Verilog templates (like
firgen's `rtl/`) — the Python generator selects architecture, binds
parameters, and generates ROMs/wrappers/vectors, but does not textually
assemble datapath code per configuration. This keeps the RTL surface small
and 100 %-coverable.

CI mirrors firgen: GitHub Actions installing `iverilog` + `verilator`, full
suite per push.

## 5. Implementation phases (each ends in a reviewable, tested state)

| Phase | Content | Done criterion |
|---|---|---|
| **P0 — skeleton** | `config.py` (validation incl. power-of-two, R\|N, width sanity), `quant.py`, `twiddles.py`, float reference, `stimuli.py`, `analysis.py`; pure-Python unit tests only | `python3 -m unittest discover` green without any simulator installed |
| **P1 — golden model R=1** | cycle-accurate `SDFGoldenModel` (pipeline regs, shifts, rounding, reorder, `ce`) + L1↔L0 SQNR tests | bit-contract frozen; SQNR numbers documented |
| **Spike S1 — BRAM inference** ✅ **done** | minimal OOC Vivado runs (Artix-7 + UltraScale+) comparing three ring-buffer codings: (a) SDP baseline — port A write / port B read, (b) single-port read-old/write-new same-address, (c) two independent delay lines sharing one true-dual-port RAM via (b) on both ports. Record LUTRAM/RAMB36 counts per variant | **(c) infers as exactly 1× RAMB36/E2 on both families, 0 LUTRAM** — pairing heuristic proven by pure inference. Bonus findings: `block` hint pins even 64-deep lines off LUTRAM (apply hints conditionally); same-address R/W never maps to URAM, split-pointer SDP does (`ram_style="ultra"` → URAM288) giving deep lines a second clean variant. Full data: `doc/mem_cutoffs.md` |
| **P2 — RTL R=1** ✅ **done (Verilator)** | `rtl/fft_sdf.v`, `tb/tb_fft_sdf.cpp`, `src/fft_gen.py`; bit-exact vs `SDFGoldenModel` under Verilator; `ce`-freeze/reset suites | full L2+L3 green for the R=1 matrix: N=2..128, fwd/inv, widths 8..25, decimals, explicit/zero scaling, periodic/pseudo/bursty freeze, multi-frame (70 tests total, incl. 20-config RTL matrix). Icarus/Questa runs deferred |
| **P3 — orders + inverse** ✅ **done (Verilator)** | DIT topology (TOPOLOGY param, mirror datapath with its own quantization contract), `fft_reorder.v` ping-pong, `fft_top.v` wrapper; all four order corners verified bit-exact N=2..128, fwd/inv, freeze, widths (RTL matrix 26/26, suite 85 tests). IFFT via conjugated twiddles already covered. |
| **P4 — SSR** ✅ **done (Verilator)** | golden model for the R·M composition first, then `fft_cross.v` + top wiring; bit-exact | R ∈ {2, 4, 8} verified like P2: R=4/R=8 lane-DFT networks, sigma-signed Q, split scalar multiply, 10-stage crossbar pipeline — all bit-exact fwd+inv (96 tests total). All three SSR configs closed 500 MHz post-route on KU5P (R=2 N=8 +0.075 / 24 DSPs; R=4 N=16 +0.021 / 44; R=8 N=16 +0.037 / 64). |
| **P5a — timing & memory sweeps** ✅ **done** | memory-style policy with decided cutoffs (`<=1k LUTRAM < 256kb BRAM <= URAM`, `doc/mem_cutoffs.md`), split-MREG stage multiply, NLAYERS 7→10 BRAM→DSP register absorption, crossbar input staging | KU5P @2ns post-synth MET across the R=1 ladder (N=64..8192, worst +0.163); SSR R=2 N=8192 post-route −0.021/TNS −0.152 (skew-dominated synth estimate, accepted). DSP inference audited: im-path DSPs absorb AREG=2/BREG=2/DREG=1/ADREG=1/MREG=1/CREG=1/PREG=1 |
| **P5b — export & packaging** ✅ **done** | `export_core.py` CLI (firgen-style), `fft_core.v` generated wrapper with baked parameters, `fft_params.vh`, `params.txt`, `twiddle_map.txt`, TB vectors, Vivado OOC scripts, README manifest | deliverable set complete and self-verifying: exported trees build under Verilator from the README command alone — R=1 bit-exact (both order corners, incl. the generated reorder path), SSR within documented R/2+1 tolerance with vectors identical to the suite flow; exported `synth.tcl` closes OOC on KU5P (N=32 +0.700). Suite 99 tests |
| **P6 — trivial-twiddle stages (DSP reduction)** ✅ **done** | `TRIVIAL` stage flag in `fft_sdf.v`: the DIF last two stages / DIT first two stages multiply only by W^0 (real) and W^{N/4} (±j) — single-component twiddles — and compute the product exactly in fabric (`trivial_prod`: `x·(2^td−1) = (x<<td) − x` + zero/sign select) instead of 4 DSP48s. Golden model UNCHANGED (products are exact integer arithmetic; value-identical). | bit-exact across the whole matrix (99 tests green, no skips); KU5P OOC: R=1 N=2048 44→**36** DSPs (WNS +0.113), R=2 N=2048 84→**68** (WNS −0.020, unchanged), N=64 R=1 24→16, N=4 → 0 DSPs; SSR lane engines inherit it per-engine. |
| **P7 — radix-2² folding (next DSP lever, ~2×)** ✅ **closed for DIF R=1/R>1 — 500 MHz MET at every N, ~half the DSPs, ~20% fewer LUTs** | merge each DIF stage pair (2m, 2m+1) into one R2² stage with a single general complex multiplier (3 products per 4-group instead of 4); twiddle ROM slices re-layout (stride 4^m); SSR lane engines inherit. **S5 outcome (spikes/S5_r22/): the rotation identity T[i+N/4] = ∓j·T[i] holds bit-exactly** (magnitude-first quantization), so the ±j diff combine is free and exact; **but the merged product paths change the rounding points** — the plain golden and the R2² contract differ by a few LSB (rounding placement, width-dependent) with **identical SQNR** (verified N=8..1024, fwd+inv). Appendix A's "bit-identical" claim is therefore corrected to "re-pin the golden". | **Done:** `fft_fixed_batch_r22` / `_r22_dit` (batch contracts) + `R22SDFGoldenModel` (cycle-accurate streaming: per-pair `_R22DIFStage` with the staggered 3-product schedule — y2@3D+g, y1@4D+g, y3@5D+g, read-old/write-new dram/dline re-reads, pfifo lag-D, latency 3D+1) in `golden.py`; `rtl/fft_stage_r22.v` (mirrors the model: lag-2D ram / lag-D sram+dram+dline / SDP pfifo, one shared complex multiply muxed by phase, ±j combine + trivial_prod-ready fabric); spike `rtl_check.py` verifies the RTL **bit-exact vs the contract, 32 configs** (N 4..256, fwd+inv, widths/scaling). `tests/test_golden_r22.py` 15 tests; suite 114 green. **Next:** none for the DIF core — (6) twiddle ROM into the generator + export flow ✅, (7) bit-exact Verilator suite + 500 MHz timing sweep ✅ (step 7a: the stage's 4-product path was merging into one intra-DSP ALU pass — A/B-reg → PREADD → MULT → ALU → PREG, 1.85 ns, WNS −0.020 / 480 FEP at N=2048 for EVERY r22 config; fixed by the r2 stage's two levers, both required: (i) the im/re products staggered by one clock so the im MREG reaches the re DSP's CREG, and (ii) natural-width `reg signed` operands with no hand-rolled sign-extension glue on the DSP ports; the retime is latency-neutral — +1 hop on the product path, −1 (`shift_p2`) — so the golden `_R22DIFStage` and the 3D+9 stage latency are UNCHANGED; probe + 2×2 evidence matrix in `spikes/S5_r22/dsp_probe/`, `(* dont_touch *)` tried and worse. Result: R=1 r22 post-synth **+0.187 … +0.048, 0 FEP across N=64…8192** vs r2 +0.107…+0.113 at **half the DSPs** (N=2048 36→20, N=8192 44→24) and fewer LUTs (16692→13238 at N=8192); the exported N=2048 tree simulates bit-exact (`ok: 6144 samples`) and the R=1 sweep now runs the *production* `fft_sdf_r22` top instead of the spike top. Remaining, out of P7: the SSR R≥2 limiter is the **shared crossbar** (`u_cross/g_pre pp*_reg`, same −0.020/−0.165 for both arches, so the r22 lane savings come for free) and `r22` N=2048 post-route −0.117/62 FEP on the L0→CARRY8→LUTRAM-write family, i.e. the v1 all-LUTRAM rings — both want the memory-policy/registered-read rework, not the DSP path.) (The DIT stage RTL `rtl/fft_stage_r22_dit.v` mirrors the model: vline/t1line/t2line read-old rings, one shared multiplier at the a1/a2/a3 arrivals, F₄ combine with exact ±j, H/2H/3H output queues with explicit 3H wraps; the full DIT core (leftover stage 0 first + pairs + quantizer) is verified bit-exact by `spikes/S5_r22/dit_rtl_check.py` across 56 configs.) (The streaming leftover (odd n) with the parity warm `chain_latency mod 2` and its post-warm preloads, plus the output quantizer, are now in the model and the spike top — the full core is bit-exact for all N.) Actual (swept): R=1 N=2048 36→**20**, R=1 N=8192 44→**24**, R=2 N=2048 68→**44**, R=8 N=8192 300→**204** (Spiral 2048-point reference: 40). |

**Stretch goal — run-time `N` via stage bypass (`generic DYNAMIC_N`, off by default):**
One `Nmax=8192` bitstream that can do any `Ncur=2^k≤Nmax` at run time by bypassing
stages. `r2` `13` stages / `r22` `6` pairs+`1` would all be instantiated (`max` `DSP/BRAM` as swept for `Nmax`), each stage gets a `2:1` `INTERN` mux `stage_in→stage_out` vs `stage_in→bypass` (`~22 LUT/stage` `×2` for `I/Q` → `~300 LUT` `r2` / `~150 LUT` `r22` `+50L` decode, `0` extra `BRAM/DSP`). Delay `2·Dmax` `RAM` stays (`BRAM` sized to `Nmax`, `Ncur=64` wastes `32.5→1` `BRAM`), `TWIDDLE` `ROM[Nmax]` sub-sliced (`ROM_BASE(Dcur)` runtime offset), `SCALING_PACK[Ncur]`/`LATENCY(Ncur)=Ncur+Σ(3Dcur+9/10)` and `p=(cycles-LAT)%Mcur` become run-time muxes (`~10 LUT/stage` for `wptr+1-Dcur`/`k%4Dcur`), `tlast`/`tuser` from `LAT(Ncur)`. `v1` cheaper is `export_core --num-points $N` per `N` + outside `AXI` mux (`0` in-core `LUT`, `BRAM` exact, `WNS` as swept `+0.11/-0.02`); `DYNAMIC_N` is `1×` `DSP` (`24` `r22` `8192`) vs `N=64+1024+8192` `56 DSP` separate cores, at cost of `+0.25ns` bypass mux on `product-FIFO`/`DSP_A→PREG` (`WNS -0.020→-0.32` `R=2/4`, `-0.165→-0.45` `R=8` — needs the extra `k10`/`qq→u_d` pipeline already added) and `Mcur=Ncur/R` lane `Ncur` must divide `Nmax` (`R|Ncur` still). `SSR` `R>1` same per-lane `+22 LUT/lane` (`R=8` `+176L`). `ce` stall already, `N`-switch needs `ce` low + `rst` pulse to drain (datapath has no reset). `generic DYNAMIC_N=0` keeps `v1` `localparam` `DEPTH`/`LATENCY` and `0` penalty; `1` instantiates the bypass/mux and run-time `Dcur` logic; verification is `∀ Ncur` `bit-exact` vs `golden(Ncur)` (same `R=2/4/8` `R/2+1` double-quant `+` `r22` `1–2 LSB` re-pin).

**Stretch goal — run-time `FFT↔IFFT` via `inv_i` wire (`generic DYNAMIC_INVERSE`, off by default):**
`INVERSE` today is `parameter` (`0` `FFT` `W_N^k`, `1` `IFFT` `conj(W_N^k)`, `±j` `js=−/+j`, `W_R^{rq}` second layer `W_4` signs, `SCALING_PACK` `auto=(1,)*log2(N)` identical). As `input inv_i` it is `~30 LUT` total (`N=1024 R=1 r22` `3438→3507` `+69` `+2%`, `0` `BRAM/DSP`, `WNS -0.020` unchanged — `KU5P` `2ns` `inv_ti=-ti_raw` `1 XOR` on `Q` sign + `r22` `js` `1 LUT/stage` + `cross` `W_R` `~10 LUT`): `TWIDDLE` `ROM` keeps one `FFT` table (`ti_raw`), `ti = inv_i?-ti_raw:ti_raw` (`fft_stage_r22:188`, `fft_cross:272` `wq_im`), `r22` `js` `inv_i?-d1:d1` (`256`), `cross` `W_R` `±j` (`419`/`503`/`442`) mux on `inv_i`. `TOPOLOGY` (`D=N>>s+1` vs `1<<s`, `LATENCY`, `tuser/tlast` `LAT` shift) stays as generated — `inv_i` keeps stream order (`native→bitrev` `DIF` or `bitrev→native` `DIT`, `§4`); `FFT→IFFT` as same order gives `R·x/N` (`bitrev(x)`), closing `x/N` `native→native` needs `FFT native→bitrev` `DIF` + `IFFT bitrev→native` `DIT` (canonical `TX`/`RX` pair) i.e. `1×N` `fft_reorder` after `DIF IFFT` or `DIT` variant + `TOPOLOGY` mux (the `DYNAMIC_N` `Dcur` cost `+22 LUT/stage`). `SCALING_PACK` identical, so no `σ` mux. `generic DYNAMIC_INVERSE=0` keeps `parameter` `0` penalty. `N=1024 R=1 r22` `3507 LUT` vs `3438` measured `+69` in `/tmp/rtl_dyn` `KU5P` `2ns` (`20 DSP` `WNS -0.020` same); scales `~30` `N=64` → `~120` `N=8192` (`<2%`).

**Known-open items (non-blocking):**
- Deep product-FIFOs infer as LUTRAM because the output register is muxed
  (sum path vs DOB); a dedicated DOB_REG restructure was attempted and
  reverted for a read-window off-by-one. Revisit only if the pfifo read
  path becomes binding (currently second to the intra-DSP cascade hop).
- Icarus/Questa multi-simulator runs deferred since P2.
- Corner orders with SSR (bit-reversed input lanes) not yet exercised;
  R=1 corners all green via fft_reorder.
- Artix-7 @250 MHz secondary gate not yet run (KU5P is the primary
  target; the architecture is family-portable by construction).

Deliberate sequencing choices: golden-before-RTL (P1) is the load-bearing
decision — everything downstream is then mechanical comparison. SSR comes
late because it composes already-verified pieces.

## 6. Resource/throughput expectations (design targets, verified later)

| Config | Complex mults | RAM/registers | Throughput |
|---|---|---|---|
| N=1024, R=1 | ~N (plain radix-2) | stage delay lines + ROMs per mem cutoffs | 1 frame / 1024 clk |
| N=1024, R=4 | 4× sub-FFT + R-point net | 4× smaller delay lines | 1 frame / 256 clk |

Exact DSP counts depend on family presets (25×18 / 27×24 / 18×18), reused
conceptually from firgen's DSP tables.

## 7. Decisions (all questions resolved)

1. **Stage algorithm**: fully pipelined SDF, **plain radix-2 as implemented**
   (see §2.1). Radix-2² folding deferred: it changes which cycles carry
   multipliers and must be re-proven numerically equivalent to the pinned
   per-stage shift schedule before any RTL may use it (Appendix A).
2. **Framing**: data is **AXI4-Stream-encoded minus `tready`** (§2.8);
   `tuser(0) = '1'` marks start-of-frame and `tlast = '1'` marks
   end-of-frame, **on both input and output**. Sidebands ride at fixed
   latency (no handshake). Primary purpose: off-by-one detection in
   verification — the golden model asserts marker alignment every frame.
3. **Scaling defaults**: `auto` produces a **conservative schedule that
   cannot overflow** by construction; more aggressive schedules remain
   available via explicit override for exotic use cases.
4. **Rounding per stage**: round-half-up at scaling shifts, truncate at
   products.
5./11. **Complex multiply**: **4-product split with DSP C-port chaining**
   (4 DSPs/stage), implemented and swept (datasheet: 4 × (log2 N − 2) DSPs
   at R=1 for N ≥ 8 — the last two stages are single-component-trivial,
   P6). 3-multiplier Karatsuba/Gauss forms **rejected on timing**: the
   DSP48E2 pre-adder is in series with the multiplier; the extra pre-add
   term grew the intra-DSP cascade hop 1.85 → 2.61 ns (WNS −0.78 @ 2 ns)
   and misses the 500 MHz goal (P5a evidence: §2.4). The C-port pairing
   requires the im-path one cycle ahead, absorbed by the 10-layer pipeline
   (Appendix A).
6. **Twiddle ROM**: quarter-wave compression, ±1-LSB-deterministic
   quantization contract documented in `twiddle_map.txt`.
7. **Max R**: `R ∈ {1, 2, 4, 8}` for v1.
7. **Max R** for v1 — proposal `R ≤ 8`.
8. ~~Input order semantics~~ **decided**: `input_order=bitreversed` is
   implemented by emitting the DIT-SDF topology (mirror of DIF, identical
   resources) — no reorder buffer; see §2.3.
9. ~~Backpressure style~~ **decided**: none inside the core — AXI4-Stream-
   encoded ports without `tready`, datapath gated by `ce && s_axis_tvalid`;
   backpressure = external frame FIFOs gating `ce` (see §2.8). An optional
   full-AXI4-Stream wrapper (with `tready`) is future work.
10. ~~Target families~~ **decided**: Xilinx only for v1 — Artix-7 @250 MHz
    + UltraScale+ @500 MHz as synthesis gates; other fabrics postponed
    (presets make adding them cheap later).
11. ~~Complex-multiply form~~ **resolved by measurement**: 4-product split
   ships; see 5./11. above (Karatsuba did not meet the 500 MHz gate).
12. **RTL style**: **inference only, with `ram_style`/`uram_style` hints** —
    no primitive instantiation, no XPM macros. This keeps the RTL portable to
    any synthesis engine including those without attribute support. Spike S1
    therefore has a different decision rule: if read-old/write-new pairing
    does *not* infer as RAMB36, the pairing optimization is dropped (unpaired
    SDP coding infers cleanly) — never worked around with primitives.
13. **Width-fit policy**: **warn and synthesize anyway.** Configs wider than
    the hard multiplier's ports are legal — synthesis maps them to fabric or
    cascaded DSPs; users who want the extra bits can have them.

---

## Appendix A: Derived R2-SDF schedule (golden-model notes, P1)

Working notes from implementing `src/golden.py`; these pin the schedule the
RTL must reproduce.

**Stage structure (stage s = 0..n-1, D_s = N/2^(s+1)):** alternating phases
of D_s enabled cycles each, period N:

- `COMPUTE` (D_s cycles): read delayed raw sample `d` (the OLDER sample),
  pair with input `a` (NEWER); emit `round_shift(a + d, sigma_s)` on the sum
  path; write `round_shift(cmul(d - a, T[k]), td + sigma_s)` -- **diff
  contract: OLDER minus NEWER**, matching the batch reference -- back into
  the delay line (one combined rounding shift). Pair index `i` runs
  0..D_s-1, twiddle `k = i * 2^s`.
- `PASS/FILL` (D_s cycles): emit stored products from the line, write raw
  inputs in. (This is exactly one read + one write per cycle = the two BRAM
  ports of PLAN.md 2.7.)

Reset state: all stages start in `PASS/FILL`. Warmup garbage is flushed by
`out_valid = enabled_cycle >= L`.

**Per-stage pipeline register chain (NLAYERS=10, mirrors fft_stage RTL
register-for-register; updated from the original 7-layer draft):**

| Layer | Registers | Hardware mapping |
|---|---|---|
| L0 | `d_bram`, `a_reg`, `t_reg`; RAM write (PASS) | BRAM output reg (DOA_REG); read address `raddr` modeled explicitly (always `wptr - D`, no added latency) |
| L1 | `d1/a1/t1` | DSP AREG[0]/DREG[0]/BREG[0] candidate |
| L2 | `d2/a2/t2` | DSP AREG[1]/DREG[1]/BREG[1] |
| L3 | `bfly_d/bfly_s`, `t3` | pre-adder → ADREG (diff in-DSP; sum in fabric; UNGATED — PASS values never consumed, which is what lets Vivado map D−A into PREADD) |
| L4 | `prod2/prod4` + freeze (`bfly_h/t3h`) | im-path products → MREG; re operands frozen one cycle |
| L5 | `c1/c2` + `prod1/prod3` | C-port regs (CREG): im MREGs route to the re DSPs' C ports; re products one cycle behind |
| L6 | `p`, `s3` | post-adder ALU P −/+ C → PREG |
| L7 | `comb_s/comb_p` | fabric combine (aligns sum path with product path) |
| L8 | `shift_s/shift_p` | round-half-up shift staging (ungated) |
| L9 | out + product-FIFO write | COMPUTE writes rounded product at `pwp`; PASS reads at `pr = pwp − D` (write and read share the layer so the windows align) |

The C-port pairing is the reason for the im/re asymmetry: the ALU sees
same-pair P (re product computed at L5 from frozen operands) and C (im
product captured at L5). Phase gates follow the validated
layer-k = pipe_comp[k−1] pattern; preload pack is 74 bits/stage
(pipe 9 bits). FSM warm preset: `warm_s = −(Σ D_t, t<s + NLAYERS·s)
mod 2D_s` — generalized unchanged from the 7-layer derivation.

**Every GENERAL-twiddle stage multiplies.** An earlier draft of this
appendix claimed stage 0 needs no multiplier -- that holds only for the
radix-2^2 folding, NOT for the plain radix-2 schedule; caught by the
batch cross-check (negated odd slots). P6 additionally removes the DSPs
of the stages whose twiddles are all single-component (DIF s = n−2, n−1
/ DIT s = 0, 1: the k = 0 and k = N/2^(s+2) pairs, i.e. W^0 real and
W^{N/4} = ±j only): their products are computed bit-exactly in fabric as
`x·(2^td−1) = (x<<td) − x` with a zero/sign select (RTL `trivial_prod`,
value-identical to the golden's exact cmul — the golden is unchanged).

**Latency:** first valid output after exactly `L = N` enabled cycles
(sum of D_s plus one phase-alignment cycle of the last stage -- verified
declared-vs-empirical per config). Outputs for a T-sample stream span
ticks [N, N+T-1]; draining needs N-1 trailing enabled cycles. The RTL adds
further declared register stages on top.

**Bit-reversed output:** verified N=4 by hand: stream order equals the batch
DIF output permutation (bit-reversal).

**Quantization contract per stage:**
- sum path: `a + b` exact, then `round_shift(., sigma_s)`
- multiply path: exact complex-multiply partial products (Karatsuba in
  the batch/golden reference; the RTL's 4-product split with DSP C-port
  combine is value-identical -- all forms are exact integer arithmetic),
  then ONE combined `round_shift(., twiddle_decimal + sigma_s)`
  (twiddle Q-format normalization fused with stage scaling -- this is
  the post-DSP48P shift)
- every general-twiddle stage multiplies (P6: the last two stages'
  single-component twiddles map to exact fabric products — `trivial_prod`);
  the radix-2^2 folding that removes more trivial multiplies must be
  re-proven numerically equivalent before any RTL uses it
- datapath stays Q(sample_decimal) end-to-end; with the conservative
  schedule the reported spectrum is X_true/N (amplitude-preserving)
- final output rescale + saturate via `quantize_output`

**Equivalence assumption for radix-2^2 (CORRECTED by spike S5):** the
R2^2 folding changes WHICH cycles carry general multipliers AND the
product rounding points (the ±j diff combine moves before the multiply).
The rotation identity T[i+N/4] = ∓j·T[i] holds bit-exactly in the
canonical table, so the combine itself is exact; but round_shift is not
rotation-invariant, so the merged contract differs from this model by
exactly **1 LSB** with **identical SQNR** (measured, spikes/S5_r22/).
P7 therefore RE-PINS the golden to the R2^2 contract
(`fft_fixed_batch_r22`, spike reference) rather than claiming bit-identity
with this one.
