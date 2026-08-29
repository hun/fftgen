# P8 — SSR corner orders at R = 2, radix-2² (N = 2048)

**Driver:** an existing fast-convolution core must drop in FFT (`native → bitreversed`)
and IFFT (`bitreversed → native`) engines at **R = 2, N = 2048, stage_mode = r22**.
Scope deliberately excludes R = 4/8 and the r2 arch (see §5).

## STATUS (both directions shipped; inverse RTL CLOSED)

| step | state | evidence |
|---|---|---|
| 0 config/contract guards | DONE | `config.SSR_CORNER_ORDERS`; the r2 leak closed; `tests/test_config.py` |
| 1 golden model | DONE | `_SSRLane(reorder_out=)`, `emit_brev`, `ssr_emission_perm`; `tests/test_golden_ssr_orders.py` |
| 2 RTL, forward `native -> bitrev` | DONE | `fft_cross.EMIT_BREV` + `fft_ssr_r22.REORDER_OUT`; `tests/test_rtl_ssr_orders.py` |
| 3 export + verification bar | DONE | `-GREORDER_OUT` in the sim command AND `synth.tcl`; shipped `compare.py`; sweep arch `r22b` |
| 4a corner-order IFFT, GOLDEN MODEL | **DONE** | `SSRCornerInverseModel` (transpose: crossbar-first + per-lane reorder + existing DIF-IDFT lanes); numpy identity + corner-FFT round trip; mutations caught; committed 473b54a |
| 4b corner-order IFFT, RTL | **DONE** | `rtl/fft_ssr_r22_inv.v` (transpose wrapper: R-point first, per-lane input reorder, existing DIF-IDFT lanes; 5-stage pipeline: the twiddle pairing fix + the product/combine register split the N=2048 synth needed). BIT-EXACT vs `SSRCornerInverseModel` (tol=0 in the flow), tuser/tlast positional, round trip through the corner-FFT RTL recovers x/2^log2(N); `tests/test_rtl_ssr_orders.py` + `tests/test_export.py`. Exported N=2048: `compare.py` PASS bit-exact (15314 samples); sweep arch `r22i`; timing: see doc/datasheet.md |
| 5 datasheet/README refresh for `r22b` | **done** | full 60-config sweep (arch `all`), r22b section in doc/datasheet.md |

Usable today, BOTH directions at R=2, N=2048, r22: **FFT `native -> bitreversed`**
and **IFFT `bitreversed -> native`** -- both exported, self-verifying
(`compare.py` PASS bit-exact, tolerance 0), and both gate-verified end to
end (values + tuser/tlast positional; the FFT corner -> IFFT corner round
trip recovers the input at 2^-log2(N)). The inverse's only extra cost over
the forward corner is the two per-lane input reorders (~4 BRAM36 at
N=2048); the r22 DIT lane remains the optional memory optimization.

## BUGS FOUND BUILDING THIS (all pre-existing, all fixed)

Each was invisible to a green suite. Written down so the blind spots get
checked, not just so the fixes are credited.

### 1. SSR output markers ran 4 clocks early -- both arches, every R

`fft_ssr.v` and `fft_ssr_r22.v` each re-timed `tuser`/`tlast` with a
hard-wired 3-tap shift register whose comment asserted "same depth as the
crossbar (CB_LAT = 3 stages)". The crossbar was 3 stages *once*: the P5a
fabric input register and the R>=8 stages made CB_LAT 7 and 11, and the marker
pipe never followed. Every SSR core therefore emitted its frame-boundary
markers 4 clocks (8 at R=8) before the frame data they labelled.

Found by accident while comparing per-bin values between two netlists (the
mismatch was a rotation, not an error). Proven by signature rather than by
argument: measured skew was -4 clocks at R=2, -4 at R=4 and **-8 at R=8** --
exactly `CB_LAT - 3`, and R=8 is the case where CB_LAT differs. Both arches
reproduced it.

Why three phases of green tests missed it: `generate_ssr` checked markers by
**counting** one SOF + one EOF per N-sample group, and a count is invariant
under precisely this bug. Fix: markers enter `fft_cross` as `in_user`/
`in_last` and ride the datapath's own CB_LAT-stage pipeline, so they cannot
diverge again without also changing the data path; the flow now compares
markers **positionally**. The contract never changed, so no golden model or
vector was re-pinned -- the RTL simply did not match what it was claimed to be
verified against. Docs/commits saying SSR is "bit-exact (values + tuser/
tlast)" were true for values and were NOT true for marker positions until now.

### 2. `export_core` shipped a synth script building a different core than the sim

`vivado/synth.tcl` for the SSR r22 top never passed `-generic REORDER_OUT`, so
a corner-order export would simulate the bitrev core while **synthesizing the
default native one** -- a netlist that cannot produce the shipped
`expected.txt`. Fixed. The guard is a test that compares the two artifacts
against each other (`test_sim_and_synth_agree_on_emission_order`), because the
general shape of the bug is one contract stored in two places; re-verifying
either against the model would not have caught it.

### 3. The exported testbench cannot distinguish right from wrong

The tb streams stimulus, dumps `actual.txt`, and prints `ok: N samples` for ANY
completed run -- by design, since the golden model is the source of truth. But
the tree shipped the comparison rule as *prose* ("SSR compares with tolerance
R/2+1 LSB after word-offset alignment") with no executable, so a customer
following README.txt could not tell a correct core from a wrong one.
Demonstrated rather than assumed: the corner-order tree built with the wrong
generic mismatched **10883 of 11230** samples and the tb still printed `ok`.
Fix: every exported tree ships `compare.py` (word-offset alignment, the
documented value tolerance, positional markers) and README.txt marks it
REQUIRED.

Correction to my own earlier note in this session: "ran the export, got
`ok: N samples`" is not verification. Measured properly afterwards: R=1 IS
bit-exact (0 mismatches, 0 marker mismatches at N=2048); SSR is within
tolerance (max 1 LSB over 12254 samples at N=2048) and NOT bit-exact -- which
is what README.txt already said, and what `compare.py` now prints in the tree.


## 1. What blocks it today (three layers, all explicit)

| layer | behaviour |
|---|---|
| `src/config.py:140` | r22 + `ssr>1` requires `output_order='native'` → `ValueError` |
| `src/fft_gen.py:164` | `generate_ssr` hard-`assert`s native → native |
| `src/golden_ssr.py:111-114` | `NotImplementedError("SSR v1 supports native input/output only")` |

Separate bug: `config.py` **accepts** `ssr=2` + corner orders for the **r2** arch,
which then dies on the `fft_gen` assert. Should raise like r22 does (§4, step 0).

## 2. The ordering algebra (verified numerically, R = 2, N = 2048)

`N = R·M`, `n = qM + p ⇄ q = n≫log2M, p = n mod M`; the forward SSR computes
`A_r[p] = DFT_M(x[R·j+r])`, then `X[qM+p] = DFT_R(A_r[p]·W_N^{rp})`. Hence

```
bitrev_N(R·c + q) = bitrev_M(p=c)·R + bitrev_R(q)          (*)
```

Two consequences, both checked against `numpy` above:

1. **R = 2 makes `bitrev_R` the identity.** The lane axis needs *no* permutation
   in either direction — the whole corner-order problem collapses to the `M`
   axis inside each lane. (At R = 4/8 `bitrev_R` is `(0 2 1 3)` / interleave,
   which is **not affine mod R**, so it cannot be absorbed into DFT wiring —
   that's why R > 2 is out of scope, §5.)
2. Feeding/emitting a lane's bins in `bitrev_M` order is exactly what a DIF
   core does with its reorder **off** (and what a DIT core consumes) — so
   `bitrev_N` at the N level costs **no new memory** at R = 2, only a
   reordered read/write of the existing lane buffers.

## 3. The two builds

### 3a. FFT `native → bitreversed` — small, latency-reducing, *cheaper* than today

The core already emits `X[qM+p]` with `p` produced by the crossbar's own counter
(`scnt`, `rtl/fft_cross.v:118`; ROM read `wn_rom[gr*M + p]`, :108). Per (*) we
need the frame emitted so that word `(c, q_out)` carries `X[bitrev_N(2c+q_out)]`
= `X[q_out·M + bitrev_M(c)]` — i.e. keep the block/lane mapping, walk `p` in
`bitrev_M` order:

1. `fft_ssr_r22` lanes: `.REORDER_OUT(1)` → **`(0)`** (DIF lane cores, no
   per-lane reorder buffer — `fft_top_r22` already has the parameter and the
   R = 1 r22 contract uses exactly this configuration).
2. `fft_cross`: derive the ROM/emission index as `p = bitrev(MW)'(scnt)` instead
   of `scnt`. Pure wiring + a constant; `WN_FILE` stays as generated.
3. Markers/`tlast`: `mature`/`scnt` comparisons unchanged (bitrev is a
   permutation of the same clock range), so `CB_LAT` and the `pd` taps stand.

**Cost:** removes 2 × M × 2·W bits of lane reorder RAM per lane (N = 2048,
R = 2: −2 × 2048 × 32 b ≈ −4 BRAM36-equivalents) and shortens lane latency —
a *contract change* (`SSRGoldenModel.latency`), which is fine for a new core
but must be exported in `params.txt`. Timing: strictly less in the critical
path (the reorder RAM's ~1.25 ns clock-to-out hop disappears from the lane
output — the very thing that forced the `q/d` register in P5a); re-sweep to
confirm, expect ≥ today's `+0.015`.

### 3b. IFFT `bitreversed → native` — the transpose route (as built)

Per (*) the incoming word `(c, q)` carries `X[qM + bitrev_M(c)]` — correct block
assignment, `M`-reversed local index. The inverse is the **transpose** of the
forward network, so the R-point step runs FIRST rather than last. Choosing the
transpose makes the r22 DIT lane unnecessary: at R=2 the R-point inverse is
add/sub, and the arriving word already carries both q values at the same p, so
per clock:

    a0 = round_shift(x0 + x1, 1)               -> lane 0 (twiddle W^0 = 1)
    a1 = round_shift(x0 - x1, 1) * conj(W_N^p) -> lane 1

then a per-lane bitrev→native reorder (the EXISTING `fft_reorder`) feeds the
EXISTING verified M-point DIF-IDFT lane (`fft_top_r22`, TOPOLOGY=0, INVERSE=1,
REORDER_OUT=1). Output slot e ↦ x[e] (flat native), so FFT(corner) ∘
IFFT(corner) is the identity — see `SSRCornerInverseModel` (committed 473b54a).

- Model: DONE (numpy identity + corner-FFT round trip; mutations caught).
- RTL: **DONE** — `rtl/fft_ssr_r22_inv.v` (committed). The two real bugs the
  first bring-up could not isolate: (1) the twiddle ROM read happened one
  stage late, pairing word c's twiddle with word c-1's data (fixed by the
  S0 ROM read + S1 coefficient hop `wa -> wq`); (2) the a0/a1 add/sub lags
  the twiddle chain by one generation, so the lane-1 product must pair with
  `a1` at the SAME stage (the model's `b1` divergence was this pairing, not
  the round/saturate). The wrapper is a 5-stage pipeline (input -> add/sub+
  twiddle -> partial products at the DSP MREG -> fabric combine -> quantize)
  -- the product/combine register split is what the N=2048 synth needed
  (-2.24 ns -> -0.53 ns post-synth; see doc/datasheet.md for the post-route
  number). BIT-EXACT vs the golden model (tol=0), markers positional, and
  the RTL round trip FFT(corner) -> IFFT(corner) recovers x/2^log2(N).
  Debugging post-mortem: `doc/lessons_debugging.md`.
- Memory: the two per-lane input reorders cost ~4 BRAM36 at N=2048. If that
  matters, the r22 DIT lane (P7 stretch item) removes them entirely at the
  price of new butterfly topology + its own DSP/timing pass — an optimization
  option, not the plan.

## 4. Verification gates (same bar as P7 — nothing ships without all four)

| # | gate | tool |
|---|---|---|
| 0 | r2 order-guard fix + regression that corner orders fail *cleanly* outside the verified subset | `tests/test_config.py` |
| 1 | golden model self-consistency: `nat→bitrev` FFT ∘ `bitrev→nat` IFFT = scaled `x` in native order (the canonical TX/RX pair — this is the property the convolution core relies on) | new `tests/test_ssr_orders_model.py` |
| 2 | bit-exact RTL vs golden (values **and** `tuser`/`tlast`) for both new cores, incl. the R = 2 lane↔block algebra at every size the suite already uses (N = 8…64) and **N = 2048 R = 2 export sims** | `tests/test_rtl_ssr_r22.py` extended + `export_core` (the flow used today's spot checks: `ok: 12254 samples`) |
| 3 | timing/area: sweep R = 2 N = 2048 both directions at 500 MHz post-synth, post-route the corner; update `doc/datasheet.md` | `python3 -m src.datasheet_sweep --arch r22` |

## 5. Explicitly out of scope (say so in the datasheet, don't leave it ambiguous)

- **R = 4 / R = 8 corner orders**: `bitrev_R` is not affine, so it cannot be
  wired; those need a real R-wide `bitrev_N` buffer (`fft_reorder` is 1 word per
  clock today, so it would throttle R = 4/8 back toward 1 sample/clock) plus the
  URAM-bank collision rule re-checked. Separate phase, separate quote.
- **r2 arch corner orders**: likely free once the plumbing exists (its DIT
  lanes already exist from P3, and they would also make the inverse route
  reorder-free) — do it as a follow-on *reuse* win, not now.
- **R = 2 at other N**: should come for free from the same generators; verify
  N = 512 / 4096 as a sanity pair in gate 2, but don't re-sweep the world.

## 6. Rough effort

| item | estimate |
|---|---|
| step 0 (guard fix) + FFT side 3a + gates 0–3 | **DONE** — the forward half shipped (exported, swept, documented) |
| inverse model (transpose route, 3b) | **DONE** — `SSRCornerInverseModel`, committed 473b54a |
| inverse RTL (3b) | **DONE** — wrapper shipped, flow bit-exact (tol=0), round-trip RTL test, export self-verifies at N=2048, sweep arch `r22i` run; timing in doc/datasheet.md |
| r22 DIT lane (optional, removes the ~4 BRAM36 of reorders) | 1–2 days — only if memory is the binding constraint; not required by the plan |

## 7. One integration question that could shrink this further

In an `FFT → spectral MAC → IFFT` chain the intermediate permutation only
matters if something else is keyed to absolute bin indices. If their spectral
buffer's write address is simply "whatever order the FFT emitted" and `H[k]` is
a ROM whose *content* is generated offline, then the permutation cancels and
today's **verified** `native → native` pair (N = 2048 R = 2: `ok: 12254 samples`
both directions, 44 DSP / 7 787 LUT / 12 BRAM, WNS +0.015 @ 500 MHz post-synth)
works after re-generating `H` in the matching order — zero RTL work. Worth
asking before committing 3 days. But if their address generators, channel
masks or existing `H` ROM are fixed in bitrev order (likely, since the core is
finished), 3a + 3b is exactly what's needed.
