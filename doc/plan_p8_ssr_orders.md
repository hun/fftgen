# P8 — SSR corner orders at R = 2, radix-2² (N = 2048)

**Driver:** an existing fast-convolution core must drop in FFT (`native → bitreversed`)
and IFFT (`bitreversed → native`) engines at **R = 2, N = 2048, stage_mode = r22**.
Scope deliberately excludes R = 4/8 and the r2 arch (see §5).

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

### 3b. IFFT `bitreversed → native` — needs the r22 DIT lane (the real work)

Per (*) the incoming word `(c, q)` carries `X[qM + bitrev_M(c)]`: correct block
(lane) assignment, `M`-reversed local index. But the inverse factorization that
the current wrapper implements is *lane-transform first*, so a `bitrev_M`-ordered
bin stream must be consumed by an engine that turns bit-reversed input into
native output — i.e. an **M-point radix-2² DIT lane**, which does not exist:
`config.py:132` says `stage_mode='r22' is DIF-only`, and PLAN lists "r22 DIT
into the generator" as the P7 stretch goal.

Steps:
1. **Fold DIT into `fft_stage_r22` / `fft_sdf_r22`** (the P7 stretch item):
   the r2² pair butterfly is its own transpose modulo twiddle conjugation, so
   this is expected to be a re-indexing of the existing stage + `fft_twiddles`
   for the DIT exponent map, not a new datapath. Reuse the P3 `build_dit`
   route as the template, and the step-7 DSP lessons (natural-width
   `reg signed` operands + staggered im/re) from day one — that path already
   closes at 500 MHz in the DIF direction.
2. **SSR wrapper order plumbing**: lane `.REORDER_OUT(0)` + a bit-reversed
   *input* index, i.e. the lane stream feeding the DIT lane in `bitrev_M`
   order (which is what the wire already carries, per (*)), and the crossbar's
   `p` counter bit-reversed for the `W_N^{+rp}` rows.
3. `SSRGoldenModel` gains `input_order`/`output_order` (drop the two
   `NotImplementedError`s), reusing `ssr_emission_to_native()` plus a
   `bitrev_N` pre-permutation on the input side.
4. `config.py`/`fft_gen.py`: lift the guards for the *verified subset*
   `{R=2} × {r22} × {nat→bitrev fwd, bitrev→nat inv}` only; everything else
   keeps raising, with the r2 gap fixed to raise properly too.

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
  lanes already exist from P3) — do it as a follow-on *reuse* win, not now.
- **R = 2 at other N**: should come for free from the same generators; verify
  N = 512 / 4096 as a sanity pair in gate 2, but don't re-sweep the world.

## 6. Rough effort

| item | estimate |
|---|---|
| step 0 (guard fix) + FFT side 3a + gates 0–3 | ~0.5 day — 3 code lines + model + sweep |
| 3b items 2–4 (SSR plumbing, model, config, verification) | ~0.5 day |
| 3b item 1 (**r22 DIT lane**) | 1–2 days — new topology, needs its own DSP/timing pass (P7 needed a full day of probing to find the MREG fix; expect the same class of work here) |
| **total** | **~2.5–3 days** |

The forward half (3a) is the cheap, immediate one and is *better* than the
current core in area and latency; the IFFT half is where the cost genuinely is.

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
