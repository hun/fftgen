# fftgen

A Python generator that emits a finished, parameterized **streaming FFT/IFFT
Verilog core** for Xilinx FPGAs (UltraScale+; family-portable by
construction). One configuration in, a complete self-contained deliverable
set out: RTL, twiddle ROMs, a baked-parameter top-level wrapper, parameter
contract files, verification vectors, and Vivado OOC scripts.

The project follows a model-first methodology (mirroring
`~/Projects/firgen_folding`): a pure-Python, cycle-accurate, **bit-exact
golden model** pins the numerical contract before any RTL exists; every RTL
result is verified bit-exact against that model.

## Status

All implementation phases (P0–P6) are complete; see the phase table in
[PLAN.md](PLAN.md) §5.

- **R = 1 (SDF)**: bit-exact N = 2…128 (all four input/output order corners,
  fwd/inv, widths 8…25, `ce`-freeze suites); 500 MHz met post-synth on KU5P
  for N = 64…8192 (worst WNS +0.107).
- **DSP reduction (P6)**: the DIF last two / DIT first two stages multiply
  only by W^0/±j and emit exact fabric products — R=1 N=2048 uses **36 DSPs**
  (was 44), R=2 N=2048 **68** (was 84), N=4 **0**; timing unchanged.
- **SSR (R = 2, 4, 8)**: bit-exact fwd+inv; R = 2 N = 8192 and the R = 4 / R = 8
  corner configs close 500 MHz post-route on KU5P.
- **Export (P5b)**: exported trees build under Verilator from the generated
  `README.txt` command alone and are bit-exact against the shipped
  `expected.txt` vectors.
- Test suite: 114 tests green (unit + golden + Verilator RTL).

Current numbers: [doc/datasheet.md](doc/datasheet.md) (N × R synthesis sweep,
KU5P @ 500 MHz).

## Requirements

- Python ≥ 3.8 (pure stdlib; `numpy` optional, via `pip install -e .[dev]`)
- Verilator (for the RTL test suites; RTL tests auto-skip without it)
- Vivado (optional, for the OOC synthesis scripts in exported trees)

## Quickstart

Run the test suite:

    python3 -m pytest tests -q        # or: python3 -m unittest discover -s tests

Export a core for one configuration:

    python3 -m src.export_core --num-points 1024 --outdir build/export
    python3 -m src.export_core --num-points 8192 --ssr 2 \
        --output-order native --outdir build/export_ssr

The output directory is fully self-contained and self-describing; start with
the `README.txt` written alongside the files:

    fft_core.v        generated top, all parameters baked in
    fft_sdf.v         radix-2 SDF stage chain (10-layer pipeline)
    fft_reorder.v     ping-pong bit-reversal reorder
    fft_cross.v / fft_ssr.v / fft_top.v   SSR builds only
    fft_twiddles.mem  twiddle ROM ($readmemh)
    fft_preloads.vh   post-warm FSM preload pack
    fft_params.vh     concrete parameters as `define macros
    params.txt        human-readable parameter record
    twiddle_map.txt   twiddle layout + quantization contract
    tb/               Verilator testbench (C++)
    stimulus.txt / expected.txt   verification vectors
    vivado/synth.tcl  OOC synthesis script (IMP=1 adds P&R)

Simulate an exported core (from its directory; full command in `README.txt`):

    verilator --cc --exe --build -j 4 --top-module fft_top \
      -Wno-fatal +define+FFTGEN_PRELOADS +incdir+. \
      -CFLAGS "-DTB_SAMPLE_WIDTH=16 -DTB_OUTPUT_WIDTH=16" \
      fft_core.v fft_sdf.v fft_reorder.v tb/tb_fft_sdf.cpp
    ./obj_dir/Vtop        # writes actual.txt

`expected.txt` vs `actual.txt`: bit-exact for R = 1; SSR compares with a
documented R/2 + 1 LSB tolerance after word-offset alignment.

## Configuration

Key parameters (full table and defaults in [PLAN.md](PLAN.md) §1):

| Parameter | Meaning |
|---|---|
| `num_points` | N, power of two (≥ 2) |
| `ssr` | R = samples per clock, power of two dividing N (1…8) |
| `inverse` | FFT / IFFT |
| `input_order` / `output_order` | `native` or `bitreversed` (output default: bitreversed) |
| `sample_width` / `sample_decimal` | signed input format (I/Q, both components) |
| `output_width` / `output_decimal` | signed output format (defaults to input) |
| `twiddle_width` | signed twiddle format (narrow, rides the DSP B port) |
| `scaling` | per-stage shift schedule: `auto` (conservative, provably overflow-free) or explicit 0/1/2-bit list |

Fixed policy: round-half-up at scaling shifts, truncate at products. The
`auto` schedule keeps the datapath in Q(sample_decimal) end-to-end; with it
the reported spectrum is X_true/N (amplitude-preserving).

Memory style is automatic via empirically derived cutoffs
([doc/mem_cutoffs.md](doc/mem_cutoffs.md)): ≤ 1024 bits distributed LUTRAM,
< 256 kbit block RAM (RAMB36/E2), above that URAM288; twiddle ROMs go to
block RAM from N ≥ 256.

## Interface

AXI4-Stream signal naming and packing **without `tready`** — no backpressure
anywhere inside the core. Ports: `tdata` (byte-aligned I/Q lanes), `tvalid`,
`tuser[0]` = start-of-frame, `tlast` = end-of-frame, on both slave and
master sides; `ce` gates the pipeline (freeze/reset). One frame = N complex
samples; steady-state throughput is **one frame per N/R clocks** while
`ce = 1`, back-to-back capable. If the surrounding system needs
backpressure, buffer full frames in FIFOs in front of/behind the core and
gate `ce`.

Non-goals for v1: arbitrary (non-power-of-2) N, fractional L/M resampling,
block floating point, runtime twiddle reload.

## Documentation map

| Doc | What it covers |
|---|---|
| [PLAN.md](PLAN.md) | goals & scope, architecture, verification strategy, phase status, all design decisions, golden-model derivation (Appendix A) |
| [doc/architecture.md](doc/architecture.md) | how the core works: SDF stage operation, the 10-layer pipeline, complex multiply, ordering, SSR crossbar, fixed-point/memory/reset contracts |
| [doc/datasheet.md](doc/datasheet.md) | N × R resource/timing sweep (KU5P @ 500 MHz), how to read the table, timing notes, memory policy summary |
| [doc/mem_cutoffs.md](doc/mem_cutoffs.md) | memory-style cutoff decisions with the underlying OOC synthesis experiments (S1–S4) |
| `spikes/` | experiment workspaces and findings (timing, DSP cascade) |
| generated `README.txt` | per-export build/simulate/synthesize instructions |

## Repository layout

    src/            generator: config, golden models (R=1 + SSR), RTL
                    generation, core export (CLI: src/export_core.py)
    rtl/            the shared RTL (fft_sdf, fft_cross, fft_ssr,
                    fft_reorder, fft_top)
    tb/             Verilator testbenches (C++)
    tests/          pytest/unittest suites (unit, golden contract, RTL)
    build/          generated outputs (gitignored)
    doc/            datasheet + memory cutoff documentation
    spikes/         Vivado/Verilator experiment workspaces

## Known-open items (non-blocking)

Tracked in [PLAN.md](PLAN.md) §5: deep product FIFOs infer as LUTRAM
(output-register mux); Icarus/Questa runs deferred since P2; corner orders
with SSR not yet exercised; Artix-7 @ 250 MHz secondary gate not yet run.

## Licence

This project is dual-licensed. You may use, study, modify, share and
distribute it under either of the following, at your option:

1. **CERN Open Hardware Licence Version 2 — Strongly Reciprocal
   (CERN-OHL-S-2.0)** — the full text is in [LICENSE](LICENSE) and at
   <https://ohwr.org/cern_ohl_s_v2.txt>. Under this strongly reciprocal
   licence, products built from these designs (or modified versions of
   them) must remain available as open hardware under CERN-OHL-S-2.0.
2. **A closed-source commercial licence**, available on request from
   **Hannes Klas &lt;hannes.klas@gmail.com&gt;** — for projects where the
   reciprocity obligation does not fit.

SPDX expression: `CERN-OHL-S-2.0 OR LicenseRef-Commercial`.

Versions in the project history prior to the relicensing commit were
distributed under a proprietary licence and remain governed by that licence.
