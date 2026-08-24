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

### 2.1 Baseline core (R = 1): fully pipelined radix-2² single-path delay feedback (SDF), DIF

**Decision:** a *fully pipelined streaming* core — not a buffered/macro
engine that ingests a frame, computes, and emits. The chosen topology is the
classic radix-2² DIF-SDF pipeline (Takala/Baas style), selected over the
alternatives on resource grounds:

| Topology | Multipliers | Memory | Streaming? |
|---|---|---|---|
| **SDF (chosen)** | ~N/4 complex | ~N delay | yes, continuous |
| MDC / R2²SDF variants | same order | ~2–3× more (commutators) | yes |
| Buffered/burst (in-place array) | few, time-multiplexed | full frame RAM ×2 + controller | no — dead cycles between frames |
| Fully parallel | O(N log N) | minimal | yes but absurd area for large N |

SDF gives the minimum-multiplier *and* near-minimum-memory continuous-stream
design; MDC only wins when R > 1 needs multiple streams anyway (§2.4 covers
that case by composing SDF engines).

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
- `log2(N)` pipeline stages. Each radix-2² stage contains one complex
  butterfly pair and **at most one** complex multiply (vs. one per radix-2
  stage) — roughly N complex multiplies saved per transform vs. plain
  radix-2 SDF, at identical delay-line cost.
- Per-stage feedback delay line of depth `N/(4·stage)` realized as an SRL/FF
  shift register (BRAM variant only if long-N becomes a real use case — same
  trade-off study as firgen's BRAM delay line).
- Twiddles stored in quarter-wave-compressed ROMs sized `~N/4` total
  (decision §7.6; exact ROM partitioning is a twiddle-module detail,
  documented in `twiddle_map.txt`).
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
- **Complex multiply = 3 DSPs (Karatsuba, decided §7.5)** via the identity
  `m1 = c·a`, `m2 = d·b`, `m3 = (a+b)·(c+d)` →
  `re = m1 − m2`, `im = m3 − m1 − m2` (numerically verified). The `a±b`/
  `c+d` terms come free from the DSP pre-adders (E1/E2). DSP conservation
  beats the extra accumulation layer; P5 still measures the timing impact,
  but there is no 4-DSP variant planned.
- Pipeline registers follow the hard macro: A/B/AREG/BREG/MREG/PREG, matching
  firgen's proven floorplaining-free closure approach.
- DSP count formula (reported by the generator, asserted by a resource test):
  `3 · Σ_stage(multiplies in stage)` — for radix-2² SDF ≈ `3N/4` per
  transform for R=1 (radix-2² halves the multiplies vs. plain radix-2).

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
│   ├── fft_sdf.v            R=1 radix-2² SDF core (all stages, parameterized)
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
| **Spike S1 — BRAM inference** *(early P2, blocks the pairing heuristic)* | minimal OOC Vivado runs (Artix-7 + UltraScale+) comparing three ring-buffer codings: (a) SDP baseline — port A write / port B read, (b) single-port read-old/write-new same-address, (c) two independent delay lines sharing one true-dual-port RAM via (b) on both ports. Record LUTRAM/RAMB36 counts per variant | report in `doc/mem_cutoffs.md`: does (c) infer as 1× RAMB36? **Decision rule (§7.12, inference-only):** if (c) fails to infer, the pairing optimization is dropped in favor of the cleanly-inferring SDP coding — no XPM/primitive workaround |
| **P2 — RTL R=1** | `fft_sdf.v`, `fft_reorder.v`, `fft_top.v`; Verilator + Icarus bit-exact; `ce`-freeze/reset suites | full L2+L3 green for the R=1 matrix |
| **P3 — orders + inverse** | DIT topology variant (bitrev-in/native-out), IFFT shift schedules; reorder buffer only for the native→native corners; coverage sweep extended | L2–L4 green across topology/order/inverse matrix |
| **P4 — SSR** | golden model for the R·M composition first, then `fft_cross.v` + top wiring; bit-exact | R ∈ {2, 4} verified like P2 |
| **P5 — export & sweeps** | `export_core.py` CLI, `fft_params.vh`, `twiddle_map.txt`, Vivado OOC scripts (`vivado_export.py`, firgen-style), **resource/memory-cutoff sweep → `mem_policy.py` final values**, README, CI | deliverable set complete; `doc/mem_cutoffs.md` backs every `auto` constant; timing met on Artix-7 @250 MHz and UltraScale+ @500 MHz |

Deliberate sequencing choices: golden-before-RTL (P1) is the load-bearing
decision — everything downstream is then mechanical comparison. SSR comes
late because it composes already-verified pieces.

## 6. Resource/throughput expectations (design targets, verified later)

| Config | Complex mults | RAM/registers | Throughput |
|---|---|---|---|
| N=1024, R=1 | ~N (radix-2²) | ~1.5N stage delay FFs + ROM | 1 frame / 1024 clk |
| N=1024, R=4 | 4× sub-FFT + R-point net | 4× smaller delay lines | 1 frame / 256 clk |

Exact DSP counts depend on family presets (25×18 / 27×24 / 18×18), reused
conceptually from firgen's DSP tables.

## 7. Decisions (all questions resolved)

1. **Stage algorithm**: fully pipelined radix-2² DIF-SDF (see §2.1); plain
   radix-2 kept as a possible debug fallback only.
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
5./11. **Complex multiply**: 3-DSP Karatsuba form (`m3 = (c+d)·(a−b)` via
   the pre-adder) — DSP conservation wins over the extra accumulation layer;
   timing is still measured in P5 but there is no 4-DSP variant planned.
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
11. **Complex-multiply form**: default 3-DSP (pre-adder) with 4-DSP as
    fallback/timing escape — or measure first and pick per preset?
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

- `COMPUTE` (D_s cycles): read delayed raw sample `d`, pair with input `a`;
  emit `round_shift(a + d, sigma_s)` on the sum path; write
  `round_shift(cmul(a - d, T[k]), td + sigma_s)` (single combined rounding
  shift) back into the delay line. Pair index `i` runs 0..D_s-1,
  twiddle `k = i * 2^s`.
- `PASS/FILL` (D_s cycles): emit stored products from the line, write raw
  inputs in. (This is exactly one read + one write per cycle = the two BRAM
  ports of PLAN.md 2.7.)

Reset state: all stages start in `PASS/FILL`. Warmup garbage is flushed by
`out_valid = enabled_cycle >= L`.

**Latency:** first valid output after `L = N - 1` enabled cycles (sum of
D_s). The RTL adds one register per stage (declared constant, +n).

**Bit-reversed output:** verified N=4 by hand: stream order equals the batch
DIF output permutation (bit-reversal).

**Quantization contract per stage:**
- sum path: `a + b` exact, then `round_shift(., sigma_s)`
- multiply path: exact Karatsuba products, then ONE combined
  `round_shift(., twiddle_decimal + sigma_s)` (twiddle Q-format
  normalization fused with stage scaling -- this is the post-DSP48P shift)
- stage 0 structurally has no multiplier (`k = 0` for all pairs); stages
  s >= 1 multiply unconditionally, including their `k = 0` entries
  (uniform-datapath decision, documented bias)
- fractional bits evolve as `f_s = sample_decimal - sum(sigma[:s])`; final
  output rescale + saturate via `quantize_output`

**Equivalence assumption for radix-2^2:** the R2^2 folding changes only
WHICH cycles carry general multipliers, never the recursion or the rounding
points (same butterflies, same per-stage shifts). Any R2^2 RTL computing the
same recursion with the same per-stage shift points is bit-identical to this
model; pinned by the batch/stream cross-checks.
