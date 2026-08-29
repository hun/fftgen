# Debugging discipline -- lessons from the P8 corner-order bring-up

The P8 inverse-half RTL bring-up (SSR corner order `bitreversed -> native`,
R=2) produced precisely one *real* RTL bug, and roughly a dozen self-inflicted
probe bugs that masqueraded as it for hours. This document records both so the
next debug session -- on any project, not just fftgen -- starts from the traps
instead of rediscovering them.

## The technical summary (what is actually proven)

In-situ signal capture (`verilator --public-flat-rw`, reading signals per
cycle from a C++ harness) proved bit-exact in the working netlist:

- the wrapper's `a0`/`a1`/`wa` registers, RE and IM;
- the reorder's outputs `ro0`/`ro1` (the native-order lane coefficients),
  RE and IM.

The one real divergence remaining: the wrapper's lane-1 *twiddled* output
`b1 = sat(round(a1 * W_N^{-p}))`. Everything upstream and downstream of that
stage is exact; the final stream diverges only because of it. Suspects, in
order: the twiddle pairing (`p` vs the `wa` prefetch) and the round/saturate
path in stage C. **The impulse test cannot see this bug**: with constant
input, `a1 = (x0-x1)/2 = 0`, so the lane-1 path (and any twiddle mistake) is
structurally unexercised. A passing "constant" test proves permute-free reuse,
never a twiddle.

## The probe traps (all were MY harness bugs, not RTL bugs)

1. **Mixed number bases.** `stimulus.txt` is hex; `expected.txt` /
   `actual.txt` are decimal. A parser that reads decimal with `int(x, 16)`
   silently corrupts every comparison (`-1533` becomes `-5427`). Mask: before
   trusting any comparison, spot-check one value by hand in both files and in
   the model. This single bug invalidated hours of "the RTL is wrong" reads.

2. **File-stomping copies.** "Copy all `.v` into a clean dir" after copying
   `fft_ssr_r22_inv.v` itself can overwrite the very file under test (the
   bypass never took effect; I "verified" the un-bypassed module). Mask:
   after any file juggling, `grep` the compiled copy for the marker string,
   and never let a second copy step follow a targeted one into the same dir.

3. **Verilog expression sizing.** In Verilog-2001, `a + b` is computed at
   the operand width (an in-range sum **wraps** at 16 bits: 30000+30000 =
   -5536) and a concatenation `{sign, value}` is **unsigned**, which turns
   the whole chain unsigned and turns `>>>` into a logical shift. The mask is
   signed-declared helper wires:
   `wire signed [SW:0] x = {in[SW-1], in};` -- and a comment so nobody
   "simplifies" it back. This was a *real* wrapper bug, fixed; the others
   below are harness bugs.

4. **Every input channel must be driven.** A C++ harness that packed only the
   RE field left the IM bus = 0, then the reorders showed `im == re` and
   looked like an RTL fault. Same for the width of the field parsed from the
   stimulus file (I parsed only the first hex token).

5. **Sign extension on packed hex.** Stimuli are stored as unsigned 16-bit
   hex (`0x8ed9` = 36569, not -28967). Feeding the unsigned value into a
   signed-path model changes the result; always `s16()` before feeding, and
   test with a small hand-built input where every expected value is known.

6. **`ok: N samples` is not a pass.** The shipped testbench only dumps
   `actual.txt`; the golden model is the source of truth. (Also discovered
   here: `compare.py` ships in every export tree for exactly this reason.)

7. **The file under test changed under me.** Several reads of the same path
   returned different contents because a background flow or a stale probe
   process rewrote it. Check `md5sum` twice across a few seconds before
   trusting that "the RTL output changed".

8. **Verify the isolation, then stop.** When every stage except one is
   proven bit-exact and the last one is a 10-line stage, the cost of
   continuing an in-session search exceeds the value of committing a broken
   tree. Revert the unfinished RTL, keep the tree green, and write down the
   exact next move (which stage, which suspect, which test would confirm).
   The follow-up is then a bounded task.

## Harnessing Verilator internals (what worked)

- `--public-flat-rw` plus `#include "Vtop___024root.h"` exposes flattened
  names like `top__DOT__u_ro0__DOT__m_re_r` on `d->rootp`. Signals inside
  generate-branch submodules may not flatten; the `/* verilator public */`
  attribute syntax on cell instances was unreliable here -- the rootp names
  worked.
- Write the probe as a small C++ main that drives the DUT and prints the
  internals; keep it OUT of the repo or in a spike dir.
- The best probe is one that prints the values you can already compute by
  hand from the stimulus (I never fully automated the frame-offset solving --
  the empirical row↔word mapping was a repeated time sink; print `cnt` next
  time).

## Golden rule this episode reinforced

The repo's "golden model first" rule is what made the inversion attitude
sustainable: the model was already committed and verified (numpy identity +
round trip) before the RTL existed, so "the RTL disagrees with the model"
was always a problem statement, never a question. When the RTL is the
untested half, trust the model and distrust the probe.

---

Last updated with the P8 inverse-half bring-up (2026-08-29). See
`doc/plan_p8_ssr_orders.md` for the task status (step 4b: open, exact
suspect staged above).