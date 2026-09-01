# Golden-model verification (independent, numpy-anchored)

**Status: all green.** `tools/verify_golden.py` runs 165 checks covering every
golden model in `src/` — plain radix-2 (`r2`) and radix-2² (`r22`), DIF and
DIT, batch and streaming, reorder/order compositions, SSR (R = 2/4/8) and the
P8 corner orders. Every model is compared **absolutely against numpy's DFT**
with the ordering conventions derived from first principles; the project's
own tests pin the models *relative* to each other (r2↔r22 few-LSB contract
delta, batch↔streaming bit-exactness, SSR↔batch tolerance), which would hide
a defect common to two compared models. This suite closes that gap.

```
python3 tools/verify_golden.py        # ~2-4 min, prints [PASS]/[FAIL] per check
```

## Verification chain

```
true DFT (numpy)
   ^  exact-mode wide-word tests: SQNR > 100 dB (floor = documented
   |  0.5-LSB/rounding contract, twiddle-independent)
   |  production-config tests: SQNR 56-81 dB (16-bit in, 18-bit Q17, auto)
   |  structural pins: tone at bin k peaks exactly at slot bitrev(k)/k
golden models (r2 + r22, all topologies, SSR, corners)
   ^  bit-exact (R=1) / R/2+1 LSB (SSR) — the project's RTL suites:
   |  tests/test_rtl_r22.py, test_rtl_verilator.py, test_rtl_ssr.py,
   |  test_rtl_ssr_r22.py, test_rtl_ssr_orders.py, test_rtl_ssr_freeze.py,
   |  test_export.py                        (all green, Verilator)
RTL cores (fft_sdf*, fft_stage*, fft_top*, fft_cross, fft_ssr*, fft_reorder)
```

## Method

* **Exact-mode wide words.** 64-bit samples, 60/120-bit twiddles, zero
  scaling schedule. The only remaining error sources are the documented
  quantization points (0.5-LSB product roundings per stage, ≤ 1-LSB twiddle
  quantization). A fully correct butterfly graph + twiddle indexing + slot
  order must then match the true DFT to ~130 dB; a wrong index, sign,
  permutation or folding blows the comparison up to the full output range.
* **Production configuration.** 16-bit samples, 18-bit Q17 twiddles, `auto`
  scaling: the SQNR you actually get at the datasheet precision.
* **Structural pins.** A single-bin tone must produce a spectral peak in
  *exactly* slot `bitrev(k)` (DIF) / `k` (DIT) out of N bins, with the
  documented residual leakage (input rounding); a shifted impulse must give
  a flat magnitude spectrum with the correct linear phase ramp. These pin
  the emission order unambiguously.
* **SSR frames.** Windows are anchored by the SOF marker (the project's own
  convention), with lead/tail zero frames; the frame grid is never assumed
  to be flat-N-aligned (see caveat 3 below).

## Coverage and measured results (2026-08-29 run)

| # | Check | Sizes | Result |
|---:|---|---|---|
| 1 | float references (`fft_float_reference`, `fft_float_radix2`) == numpy, fwd+inv | N = 2…256 | rel err ≤ 2.6e-15 |
| 2 | twiddle table (magnitude-first, Q1.(w-1)) == exact W⁠_N^k | N = 8…1024, 5 widths | magnitudes ≤ 1 + 7.8e-3; 18-bit per-entry rel err ≤ 4.7e-6 (≈2⁻¹⁸) |
| 3 | quantization primitives: Karatsuba product identity, round-half-up shift, saturation | 200-2000 random trials | exact |
| 4 | r2 batch == true DFT, exact mode (fwd+inv, DIF+DIT) | N = 4…1024 | SQNR 129-140 dB; maxerr 0.5@N=8 … 28@N=1024 (√N growth = per-stage 0.5-LSB rounding) |
| 5 | r22 batch == true DFT, exact mode (fwd+inv, DIF+DIT), tw = 60 and 120 | N = 8…1024 | SQNR 132-140 dB; **floor invariant under tw→120 ⇒ shift-limited, not twiddle-limited** |
| 4/5 | production SQNR vs true DFT (16-bit / Q17 / auto), fwd+inv | N = 16…2048 | r2: 56.3-79.8 dB; r22: 57.3-80.9 dB (r22 ≥ r2 by 0.6…2.1 dB) |
| 6 | tone pins: peak exactly in slot bitrev(k) (DIF) / k (DIT) | N=32, all bins, r2+r22 | 100%; residual leak = input rounding (−69 dB DIF, −300 dB DIT) |
| 6 | impulse pins: flat \|X\| + phase ramp | r2+r22, DIF+DIT | SQNR ≥ 90 dB |
| 7 | FFT → IFFT round trip recovers x/N | N = 16…256, r2+r22 | ≤ 2 LSB |
| 8 | streaming models == batch **bit-exact** and == DFT | N = 16…1024, r2+r22, DIF+DIT | bit-exact = True everywhere; SQNR 59-83 dB |
| 9 | `OrderedFFTModel` all four order corners == true DFT | N = 8…128 | SQNR 68-81 dB |
| 9 | `ReorderModel` == bit-reversal permutation, streaming | N = 8…128 | exact |
| 10 | freeze (ce = 0 bubbles between samples) == ungated run | N = 16…256, r2+r22 | outputs identical, markers intact |
| 11 | SSR lanes (r2 and r22) vs the R=1 batch, fwd+inv | N = 32…256, R = 2/4/8 | worst 1-2 LSB (documented tol R/2+1 = 2…5) |
| 12 | SSR corner FFT (`r22b`) == batch, slot e = bin bitrev_N(e) | N = 32…256 | worst 1 LSB |
| 12 | SSR corner IFFT (`r22i`): corner-FFT → corner-IFFT round trip | N = 32…128 | x/N recovered, worst 2 LSB |
| 13 | saturation with full-scale input, auto schedule | N = 64…2048, r2+r22 | 0 rail hits (peaks ≤ 21% of range) |

Every check in section # is per-configuration, so the table rows are the
*worst* of the listed sizes.

## Notes and caveats encountered while building the harness

1. **r2 vs r22 SQNR.** README/tests describe the r22 contract as "identical
   SQNR" (±0.5 dB). Measured against the true DFT, r22 is consistently
   *better* than r2 by +0.6…+2.1 dB (fewer product roundings in the folded
   pairs). No correctness issue; the doc wording is just loose.

2. **The exact-mode floor is the quantization contract, not twiddle error.**
   Doubling the twiddle width (60 → 120 bits) does not move the ~132-140 dB
   ceiling; the limit is the 0.5-LSB product roundings at each stage
   (absolute error grows ~√N with the partial-spectrum words). This is the
   documented fixed-point behaviour, and it is what makes the >100 dB
   exact-mode tolerances meaningful (a wrong algorithm breaks them by
   orders of magnitude).

3. **SSR frame alignment is SOF-anchored, not flat-N-aligned.** For the
   corner-order IFFT model in particular, markers and data emerge at a
   constant intra-frame phase (e.g. flat 28 mod N at N = 32); extracting
   frames at flat multiples of N yields empty/garbage windows. Anchor
   windows at the SOF positions (as `tools/verify_golden.py`'s
   `emitting_frames` does); the project RTL tests use the same convention.
   The models are self-consistent and the RTL mirrors them bit-exactly, so
   this is a harness caution, not a defect.

4. **`SDFGoldenModel.process_stream(enable=...)` drops disabled samples**
   (its outputs equal the count of enabled entries). Real ce-gating
   behaviour is exercised at the `tick()` level (frozen cycles punch no
   data in, outputs stay identical to the ungated run), which is what
   section 10 checks and what the RTL freeze suites verify against
   (`test_rtl_ssr_freeze.py`, `test_rtl_r22.py::test_freeze_masks`).

5. **r22 DIT configs cannot be built through `FFTConfig` validation**
   (r22 DIT is intentionally outside the supported subset). The r22 DIT
   batch/stream models ignore `stage_mode`, so the tests construct an r2
   config and set `cfg.stage_mode = "r22"` afterwards — matching the
   project's own test style.

## Reproduce

```bash
python3 tools/verify_golden.py                      # golden verification (this doc)
python3 -m pytest tests -q                          # full unit + RTL + export suite
# r22 + r2 RTL/export suites individually:
python3 -m pytest tests/test_rtl_r22.py tests/test_rtl_verilator.py \
       tests/test_rtl_ssr_r22.py tests/test_rtl_ssr.py \
       tests/test_rtl_ssr_orders.py tests/test_rtl_ssr_freeze.py \
       tests/test_export.py -q
```

Requires: numpy (optional elsewhere, required here), Verilator for the RTL
suites.