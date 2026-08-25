# S3 spike: behavioral DSP48E2 cascade inference

Question: does Vivado infer PCOUT->PCIN cascades from behavioral code,
and what are the register constraints on the cascade path?

## Test
4-tap signed MAC chain mixing +/- (the shape fftgen's Karatsuba tail
needs: im = m3 - m1 - m2):

    p0 <= a0*b0;
    p1 <= p0 + a1*b1;
    p2 <= p1 - a2*b2;

Synthesized OOC on xcku5p-ffva676-1-e (VHDL *and* Verilog variants).

## Result: CONFIRMED

Netlist probes (get_pins/nets on PCOUT*/PCIN*):

    p0_reg: PREG=1 MREG=0, PCOUT -> p1_reg
    p1_reg: PREG=1 MREG=0, PCOUT -> p2_reg, PCIN <- p0_reg
    p2_reg: PREG=1 MREG=0, PCIN <- p1_reg

* Behavioral inference DOES create PCOUT->PCIN cascades.
* The inter-DSP path has EXACTLY ONE register: the sender's PREG.
  (MREG=0 -- the product register alone is not on the cascade.)
* The receiver's C port is unused on cascaded hops (tied const/VCC);
  the cascade enters the ALU via PCIN, bypassing C-reg entirely.
* Mixed add/subtract along the chain is fine (ALUMODE per hop).

## Breakers observed

The last hop of the first test used `resize(p2, 42)` (36->42 bits):
Vivado did NOT cascade there -- the receiver got the sender's P output
through FABRIC into its C port instead (PCIN tied const). Cascading
requires uniform 48-bit-aligned granularity along the chain; any width
change between accumulate steps falls back to C-port routing.

## Implications for fftgen

* Karatsuba `re = m1 - m2`: DSP0 computes m1 (PREG), cascades into
  DSP1 whose ALU computes PCIN - m2 -> re lands in DSP1's PREG.
* `im = m3 - m1 - m2` is a 2-hop chain: m3 -> (PCIN-m1) -> (PCIN-m2),
  needing 3 DSPs on one cascade lane (or stay partly in fabric).
* All values riding a cascade must keep full product width (+ guard)
  until the final hop; round/saturation must happen AFTER the last
  accumulate, not between hops.

Files: rtl/dsp_cascade.vhd, rtl/dsp_cascade_v.v, tcl/synth_probe.tcl,
tcl/synth_v.tcl (probe), tcl/trace_pins.tcl (pin tracer).

## Applied to fftgen (NLAYERS=7)

Replaced Karatsuba with two 2-DSP cascade lanes at raw operand widths
(re = dre*cre - dim*cim, im = dre*cim + dim*cre). N=64 KU5P OOC synth
at 2.0 ns:

    DSPs : 40 -> 24  (6 stages x 4, each product = exactly one DSP)
    WNS  : -3.85 -> -0.020 ns

Remaining critical path is entirely intra-DSP hard fabric (sender PREG
-> PCIN -> receiver ALU -> PREG, 1.85 ns logic, zero LUTs) -- missing
by 20 ps before place/route optimization.

## Place & route confirmation (N=64, xcku5p-ffva676-1-e, 2.0 ns)

opt_design -> place_design -> phys_opt_design -> route_design on the
OOC synth checkpoint:

    WNS   : +0.090 ns  (MET)
    TNS   : 0.000      (0 / 7625 endpoints failing)
    WHWS  : +0.427 ns  (hold clean)
    Route : 4394/4394 routable nets fully routed

Post-synth WNS (-0.020) was pessimistic; phys_opt closed the slack.
Critical path after P&R: fabric carry chain from the R3 DSP operand
registers into a lane DSP's B-input register (1.48 ns).

500 MHz closure CONFIRMED. Utilization: <1% LUT/FF/DSP on KU5P.

## SSR P4 debug state (WIP, test skipped)

Integrated fft_ssr stream still misaligns vs golden despite:
* lanes verified bit-exact against golden lane streams (dbg7 flow)
* crossbar unit-verified vs python formula (unit.cpp flow)
* frame-sync gate uses pd2 (content-phase tap: dout during word w
  holds input word w-2)

Leading hypothesis: one more residual off-by-one in the fetch/multiply/
emit chain (emitted SOF position shifts by 1 word when the phase-tap
delay changes). Next step: single-word hand-verification of one emitted
frame using dbg4-style dumps with the packed-port build.
