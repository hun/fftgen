// fftgen -- one radix-2^2 DIF stage (P7), PIPELINED for timing closure,
// mirroring spikes/S5_r22/piped_model.py register-for-register (the
// L0-register retiming, verified bit-exact over N=4..2048, fwd+inv,
// sample widths 16/18/20/24, twiddle widths 16/18).
//
// The stage merges the DIF stage pair (2m, 2m+1) into one 4-sample
// group with ONE shared complex multiplier (3 products per group
// instead of 4). Group depth D = N/4^{m+1}, twiddle stride 4^m
// (pre-sliced ROM: [T[g*4^m]], [T[2g*4^m]], [T[3g*4^m]]).
//
// Pipeline (each stage = one register; the phase chain k1..k8 is the
// delayed capture phase for the per-depth write gates):
//   L0   the four async memory reads + the input + the twiddle group
//        are captured in registers BEFORE any fanout (the user
//        directive: every LUTRAM read lands in a register first), so
//        the memory-read-to-L1 path is a short reg->logic->reg hop
//   L1   butterfly from the L0 REGISTERS (the reads of the previous
//        clock): s0 = round(a+x), d = a-x; the s1 comes from the L1
//        register s0_r (the previous clock's s0), so
//        y0_raw/sd = s_r +/- s0_r (the retimed pairing), and
//        c1/c3 = d_r +/- j*dl_r
//   L2a  the DSP AREG/BREG operand capture: the product mux (k3) and
//        the BRAM-twiddle DOUT registered so the DSP48E2 uses its
//        input registers (the A/B inputs are not combinational)
//   L2b  the IM-path products (the DSP MREG) + freeze of the RE-path
//        operands. The re multiply deliberately runs ONE CYCLE LATER
//        so the DSP ALU sees a matched P (own MREG) / C (CREG, fed by
//        the im MREG) pair -- the fft_sdf (r2) proven C-port pairing.
//        Computing all four products in one clock instead lets Vivado
//        merge the re/im combine into a single ALU pass and bypass one
//        product MREG: an intra-DSP BREG->PREADD->MULT->ALU->PREG hop
//        of 1.85 ns that misses 500 MHz (P7 step 7 timing run).
//   L3   the RE-path products (MREG) + the im-path products captured
//        at the re DSPs' C-port registers (CREG)
//   L4   re/im combine (the DSP ALU/PREG)
//   L5   round-half-up staging (products by the k7-selected shift;
//        y0 from the 5-deep y0_raw chain)
//   L6   writes + output mux (per-depth gates)
//
// The product path is one hop LONGER than the pre-P7 draft (prod_im,
// prod_re, p, shift) and the old shift_p2 alignment register is GONE,
// so the absolute cycle of every externally visible event (pfifo write,
// output mux, latency 3D+9) is unchanged -- the retime is invisible to
// the golden model, which pins the per-cycle behaviour, not the layer
// distribution.
//
// DSP CODING RULE (both halves are REQUIRED for 500 MHz; proven by
// spikes/S5_r22/dsp_probe, one stage, KU5P @2 ns):
//   1. the im/re products must be staggered by one clock (above), AND
//   2. the multiply/sum operands must be declared `reg signed` at their
//      NATURAL widths with the products assigned straight into
//      `reg signed [MWB-1:0]` -- no hand-rolled sign-extension wires on
//      the multiplier operands and no width change at the combine.
// Hand-rolling the extensions adds fabric glue between the DSP ports
// and the registers it must absorb, and Vivado then merges the re/im
// combine into a single ALU pass with the product MREG bypassed: an
// intra-DSP A/B-reg -> PREADD -> MULT -> ALU -> PREG hop of 1.85 ns
// (WNS -0.020, 96 failing endpoints per stage). With the stagger alone
// or the natural widths alone it still collapses; with BOTH the DSP
// absorbs its full register budget and the stage closes (+0.187). This
// is the same trap PLAN.md records for the Gauss/Karatsuba attempt
// ("hand-rolled extensions just add fabric glue that blocks AREG/DREG
// absorption"). Marking the product registers `(* dont_touch = "true" *)`
// is NOT a workaround: it makes it worse (-0.484).
//
// Writes and depths (from the verified piped model, +1 for the
// AREG/BREG stage):
//   ram    depth 0 (raw input)        gate k    (the async read gets
//                                          the OLD at the same address)
//   sram/  depth 0 (the CURRENT      gate k1   (the arrival clock =
//   dram   combinational s_x/d_x)              the reads' capture)
//   dline  depth 0 (the CURRENT d_x) gate k1
//   pfifo  depth 7 (shift_p)         gate k8   (the operand phase,
//                                          [0,2D) U [3D,4D))
//   output mux  depth 7 (y0_r)       gate k8; pfifo lag D
//   shift select                      gate k7   (the product's shift)
//
// Pointers: rp/sp/pwp/pr are derived from the PHASE (k mod the ring
// sizes): a chained stage's phase is its OWN step index (K_PRELOAD =
// -upstream latency), so the ring addresses stay aligned with the
// model's step-index pointers regardless of the upstream warmup.
//
// Position p's value emerges at clock p + 3D + 9 (latency 3D+9).

`default_nettype none

module fft_stage_r22 #(
    parameter integer DEPTH          = 4,    // D = N/4^{m+1}
    parameter integer WIDTH          = 16,   // internal sample width
    parameter integer SIGMA0         = 1,    // sub-stage 2m shift
    parameter integer SIGMA1         = 1,    // sub-stage 2m+1 shift
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = 17,
    parameter integer ROM_BASE       = 0,    // slice base (3D per pair)
    parameter integer NPTS           = 16,   // N (full R2² ROM size)
    parameter integer INVERSE        = 0,    // W^{N/4} = -j fwd, +j inv
    parameter [15:0] K_PRELOAD       = 16'h0, // phase offset (-upstream mod 4D)
    parameter TWIDDLE_FILE           = "fft_twiddles_r22.mem"
)(
    input  wire             clk,
    input  wire             ce,
    input  wire             rst,
    input  wire signed [WIDTH-1:0] in_re,
    input  wire signed [WIDTH-1:0] in_im,
    output reg  signed [WIDTH-1:0] out_re,
    output reg  signed [WIDTH-1:0] out_im
);

    localparam integer AW = (2*DEPTH > 1) ? $clog2(2*DEPTH) : 1;
    localparam integer DW = (DEPTH > 1) ? $clog2(DEPTH) : 1;
    localparam integer KW = (4*DEPTH > 1) ? $clog2(4*DEPTH) : 1;
    localparam integer BW = WIDTH + 1;              // butterfly width
    localparam integer CB = WIDTH + 2;              // combined-diff width
    localparam integer MWB = CB + TWIDDLE_WIDTH + 1;// product width
    localparam integer PW = WIDTH + TWIDDLE_WIDTH + 4; // shift staging width
    localparam integer TD_PLUS_S1 = TWIDDLE_DECIMAL + SIGMA1;
    localparam integer TD_PLUS_BOTH = TWIDDLE_DECIMAL + SIGMA0 + SIGMA1;

    // ------------------------------------------------------------------
    // memories (distributed, async read; single rings read-old/write-new
    // at the same address; pfifo is SDP: write pwp, read pr = pwp - D)
    // ------------------------------------------------------------------
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] ram_re   [0:2*DEPTH-1];
    reg signed [WIDTH-1:0] ram_im   [0:2*DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] sram_re  [0:DEPTH-1];
    reg signed [WIDTH-1:0] sram_im  [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] dram_re  [0:DEPTH-1];
    reg signed [WIDTH-1:0] dram_im  [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] dline_re [0:DEPTH-1];
    reg signed [WIDTH-1:0] dline_im [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] pfifo_re [0:2*DEPTH-1];
    reg signed [WIDTH-1:0] pfifo_im [0:2*DEPTH-1];
    integer _i;
    initial begin
        for (_i = 0; _i < 2*DEPTH; _i = _i + 1) begin
            ram_re[_i] = {WIDTH{1'b0}};   ram_im[_i] = {WIDTH{1'b0}};
        end
        for (_i = 0; _i < DEPTH; _i = _i + 1) begin
            sram_re[_i] = {WIDTH{1'b0}};  sram_im[_i] = {WIDTH{1'b0}};
            dram_re[_i] = {WIDTH{1'b0}};  dram_im[_i] = {WIDTH{1'b0}};
            dline_re[_i] = {WIDTH{1'b0}}; dline_im[_i] = {WIDTH{1'b0}};
        end
        for (_i = 0; _i < 2*DEPTH; _i = _i + 1) begin
            pfifo_re[_i] = {WIDTH{1'b0}};  pfifo_im[_i] = {WIDTH{1'b0}};
        end
    end


    // phase counter (free-running, aligned to the input stream by
    // K_PRELOAD = (-upstream_latency) mod 4D); the delayed chain
    // k1..k7 is the capture phase at depth 1..7 (reverse-order shifts
    // in the model): during clock i, k_d = the phase at i-d.
    reg [KW-1:0] k;
    (* shreg_extract = "no" *) reg [KW-1:0] k1;
    (* shreg_extract = "no" *) reg [KW-1:0] k2;
    (* shreg_extract = "no" *) reg [KW-1:0] k3;
    (* shreg_extract = "no" *) reg [KW-1:0] k4;
    (* shreg_extract = "no" *) reg [KW-1:0] k5;
    (* shreg_extract = "no" *) reg [KW-1:0] k6;
    (* shreg_extract = "no" *) reg [KW-1:0] k7;
    (* shreg_extract = "no" *) reg [KW-1:0] k8;

    // the twiddle group of the L0 captures (=(sp-1) mod D) + the
    // delayed register (aligned with the L1 regs / the L2 mux)
    reg [DW-1:0] g_r;
    reg [DW-1:0] g_r2;

    // pointErS derived from the PHASE (the model's step-index
    // pointers): rp/pwp = k mod 2D, sp = k mod D (0 for D=1), pr =
    // (k - D) mod 2D. A chained stage's k is its own step index, so
    // these match the model even though the downstream stage started
    // counting late.
    wire [AW-1:0] rp   = k[AW-1:0];
    wire [AW-1:0] pwp  = k[AW-1:0];
    wire [AW-1:0] pr_r = k[AW-1:0] - DEPTH[AW-1:0];
    wire [DW-1:0] sp   = (DEPTH > 1) ? k[DW-1:0] : {DW{1'b0}};
    // the group of the CURRENT reads = (sp - 1) mod D (0 for D=1)
    wire [DW-1:0] g_cur = (DEPTH > 1) ? (k[DW-1:0] - 1'b1) : {DW{1'b0}};

    // phase thresholds at KW width
    localparam [KW-1:0] ONE_D   = DEPTH;
    localparam [KW-1:0] TWO_D   = 2 * DEPTH;
    localparam [KW-1:0] THREE_D = 3 * DEPTH;

    // twiddle ROM: full R2² table (N entries), this pair's slice at
    // [ROM_BASE, ROM_BASE + 3D); slice 0 = T[g*4^m] (c1, golden slot
    // y2 = j+2D), slice 1 = T[2g*4^m] (sd, golden slot y1 = j+D),
    // slice 2 = T[3g*4^m] (c3, golden slot y3 = j+3D).
    // NOTE: this stage's internal y1/y2 labels are INVERTED vs the
    // golden model's slot names (fft_fixed_batch_r22): golden y1 is the
    // sum-difference product with T[2j*base], golden y2 the diff-path
    // product with T[j*base]. The pairing below is correct; only the
    // names differ. See spikes/S5_r22/piped_model.py for the RTL
    // convention.
    //
    // the read is REGISTERED (a BRAM DOUT): every LUTRAM read must
    // land in a register before it fans into the DSP. The address is
    // selected by (k2, g_r) at one clock BEFORE the product: the
    // k2 reg at clock i = the phase at i-3 = the product's k3 at
    // i+1, and the g_r at i = the group g_r2 at i+1, so the DOUT
    // (visible at i+1) is exactly the product's twiddle.
    localparam integer ROMW = (NPTS > 1) ? $clog2(NPTS) : 1;
    (* ram_style = "block" *)
    reg signed [TWIDDLE_WIDTH*2-1:0] tw_rom [0:NPTS-1];
    initial $readmemh(TWIDDLE_FILE, tw_rom);
    localparam [ROMW-1:0] SLICE0 = 0;
    localparam [ROMW-1:0] SLICE1 = DEPTH;
    localparam [ROMW-1:0] SLICE2 = 2 * DEPTH;
    // the L2 mux selects by the k3 phase (the operand's selection
    // phase); the READ is computed one clock earlier from (k2, g_r):
    //   k3 >= 3D      -> sd  (golden y1), twiddle = T[2g*4^m] = slice 1
    //   k3 <  D       -> c1  (golden y2), twiddle = T[g*4^m]  = slice 0
    //   D <= k3 < 3D  -> c3  (golden y3), twiddle = T[3g*4^m] = slice 2
    wire phase_mux_y2 = (k2 >= THREE_D);
    wire phase_mux_y1 = (k2 <  ONE_D);
    wire [ROMW-1:0] rom_which = phase_mux_y2 ? SLICE1
                              : (phase_mux_y1 ? SLICE0 : SLICE2);
    wire [ROMW-1:0] rom_addr = ROM_BASE[ROMW-1:0] + rom_which
                               + {{(ROMW-DW){1'b0}}, g_r};
    reg [TWIDDLE_WIDTH*2-1:0] tw_dout;
    wire signed [TWIDDLE_WIDTH-1:0] tr = tw_dout
        [TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
    wire signed [TWIDDLE_WIDTH-1:0] ti = tw_dout[TWIDDLE_WIDTH-1:0];

    // round-half-up arithmetic right shift (BW domain, sums)
    function signed [BW-1:0] round_shift_bw;
        input signed [BW-1:0] v;
        input integer sh;
        begin
            if (sh <= 0)
                round_shift_bw = v;
            else
                round_shift_bw = (v + ($signed({{(BW-1){1'b0}}, 1'b1})
                                       <<< (sh-1))) >>> sh;
        end
    endfunction
    // round-half-up arithmetic right shift (PW domain, products)
    function signed [PW-1:0] round_shift_pw;
        input signed [PW-1:0] v;
        input integer sh;
        begin
            if (sh <= 0)
                round_shift_pw = v;
            else
                round_shift_pw = (v + ($signed({{(PW-1){1'b0}}, 1'b1})
                                       <<< (sh-1))) >>> sh;
        end
    endfunction

    wire signed [BW-1:0] x_re = {{(BW-WIDTH){in_re[WIDTH-1]}}, in_re};
    wire signed [BW-1:0] x_im = {{(BW-WIDTH){in_im[WIDTH-1]}}, in_im};

    // ------------------------------------------------------------------
    // combinational reads (async; read-old at the phase-derived ptrs)
    // ------------------------------------------------------------------
    wire signed [BW-1:0] a0_re = {{(BW-WIDTH){ram_re[rp][WIDTH-1]}}, ram_re[rp]};
    wire signed [BW-1:0] a0_im = {{(BW-WIDTH){ram_im[rp][WIDTH-1]}}, ram_im[rp]};
    wire signed [BW-1:0] s0_re = {{(BW-WIDTH){sram_re[sp][WIDTH-1]}}, sram_re[sp]};
    wire signed [BW-1:0] s0_im = {{(BW-WIDTH){sram_im[sp][WIDTH-1]}}, sram_im[sp]};
    wire signed [BW-1:0] d0_re = {{(BW-WIDTH){dram_re[sp][WIDTH-1]}}, dram_re[sp]};
    wire signed [BW-1:0] d0_im = {{(BW-WIDTH){dram_im[sp][WIDTH-1]}}, dram_im[sp]};
    wire signed [BW-1:0] d1_re = {{(BW-WIDTH){dline_re[sp][WIDTH-1]}}, dline_re[sp]};
    wire signed [BW-1:0] d1_im = {{(BW-WIDTH){dline_im[sp][WIDTH-1]}}, dline_im[sp]};

    // ------------------------------------------------------------------
    // L0 registers: the reads + the input captured at the posedge
    // BEFORE any fanout (every async read lands in a register first)
    // ------------------------------------------------------------------
    reg [BW-1:0] a_r_re, a_r_im;   // the ram read
    reg [BW-1:0] s_r_re, s_r_im;   // the sram read
    reg [BW-1:0] d_r_re, d_r_im;   // the dram read
    reg [BW-1:0] dl_r_re, dl_r_im; // the dline read
    reg [BW-1:0] x_r_re, x_r_im;   // the input

    // ------------------------------------------------------------------
    // L1 wires: COMBINATIONAL from the L0 REGISTERS (the reads of the
    // previous clock); the s1 = the L1 register s0_r (the previous
    // clock's s0), so y0_raw/sd pair s_r (L0) with s0_r (L1)
    // ------------------------------------------------------------------
    wire signed [BW-1:0] s_x_re = round_shift_bw(a_r_re + x_r_re, SIGMA0);
    wire signed [BW-1:0] s_x_im = round_shift_bw(a_r_im + x_r_im, SIGMA0);
    wire signed [BW-1:0] d_x_re = a_r_re - x_r_re;
    wire signed [BW-1:0] d_x_im = a_r_im - x_r_im;
    wire signed [BW-1:0] sd_re = s_r_re - s0_r_re;   // s0 - s1 (y2)
    wire signed [BW-1:0] sd_im = s_r_im - s0_r_im;
    wire signed [BW-1:0] y0_raw_re = s_r_re + s0_r_re;   // s0 + s1
    wire signed [BW-1:0] y0_raw_im = s_r_im + s0_r_im;
    // combined diffs (CB): y1 = d0 -/+ j*d1, y3 = d0 +/- j*d1
    wire signed [CB-1:0] se_d0_re = {{(CB-BW){d_r_re[BW-1]}}, d_r_re};
    wire signed [CB-1:0] se_d0_im = {{(CB-BW){d_r_im[BW-1]}}, d_r_im};
    wire signed [CB-1:0] se_d1_re = {{(CB-BW){dl_r_re[BW-1]}}, dl_r_re};
    wire signed [CB-1:0] se_d1_im = {{(CB-BW){dl_r_im[BW-1]}}, dl_r_im};
    wire signed [CB-1:0] js_d1_im = INVERSE ? -se_d1_im : se_d1_im;
    wire signed [CB-1:0] js_d1_re = INVERSE ? -se_d1_re : se_d1_re;
    wire signed [CB-1:0] c1_re = se_d0_re + js_d1_im;  // d0 + js*d1_im
    wire signed [CB-1:0] c1_im = se_d0_im - js_d1_re;  // d0 - js*d1_re
    wire signed [CB-1:0] c3_re = se_d0_re - js_d1_im;
    wire signed [CB-1:0] c3_im = se_d0_im + js_d1_re;

    // L1 registers (the butterflies/combines; captured at the posedge)
    // (shreg_extract=no: the chains must be plain FFs -- the SRL16
    // tap reads are combinational and land on the critical path)
    (* shreg_extract = "no" *) reg [BW-1:0] s0_r_re, s0_r_im;
    (* shreg_extract = "no" *) reg [BW-1:0] sd_r_re, sd_r_im;
    (* shreg_extract = "no" *) reg [CB-1:0] c1_r_re, c1_r_im, c3_r_re, c3_r_im;
    (* shreg_extract = "no" *) reg [BW-1:0] y0_raw_r_re, y0_raw_r_im;
    (* shreg_extract = "no" *) reg [BW-1:0] y0_raw2_re, y0_raw2_im;
    (* shreg_extract = "no" *) reg [BW-1:0] y0_raw3_re, y0_raw3_im;
    (* shreg_extract = "no" *) reg [BW-1:0] y0_raw4_re, y0_raw4_im;
    (* shreg_extract = "no" *) reg [BW-1:0] y0_raw5_re, y0_raw5_im;

    // ------------------------------------------------------------------
    // L2a: the DSP AREG/BREG operand capture. The product mux (k3) and
    // the twiddle are captured one clock BEFORE the multiply so the
    // DSP48E2 absorbs its input registers (A, B) as well as the MREG
    // and the PREG -- without them the twiddle/mux fan straight into
    // the multiplier in one clock (the long path).
    // ------------------------------------------------------------------
    wire [CB-1:0] m_re = (k3 >= THREE_D) ? {{(CB-BW){sd_r_re[BW-1]}}, sd_r_re}
                        : (k3 < ONE_D ? c1_r_re : c3_r_re);
    wire [CB-1:0] m_im = (k3 >= THREE_D) ? {{(CB-BW){sd_r_im[BW-1]}}, sd_r_im}
                        : (k3 < ONE_D ? c1_r_im : c3_r_im);
    reg signed [CB-1:0] m_r_re, m_r_im;           // the AREG (operand)
    reg signed [TWIDDLE_WIDTH-1:0] tr_r2, ti_r2; // the BREG (twiddle)

    // L2b: the im-path products (MREG) + the frozen re-path operands
    // (the re multiply runs one cycle later so the DSP C-port pairing
    // P -/+ C sees the same pair -- see the header)
    reg signed [MWB-1:0] prod_im_tr, prod_im_ti;
    reg signed [CB-1:0] m_rh_re;                 // frozen re operand
    reg signed [TWIDDLE_WIDTH-1:0] tr_rh, ti_rh; // frozen twiddle

    // L3: the re-path products (MREG) + the im products at the C ports
    // (CREG). re = m_re*tr - m_im*ti, im = m_re*ti + m_im*tr.
    reg signed [MWB-1:0] prod_re_tr, prod_re_ti;
    reg signed [MWB-1:0] c_ti, c_tr;

    // ------------------------------------------------------------------
    // L4: re/im combine (the DSP ALU/PREG)
    // ------------------------------------------------------------------
    reg signed [MWB-1:0] p_re, p_im;

    // ------------------------------------------------------------------
    // L5: round-half-up staging (the k7-selected shift; the delayed y0)
    // ------------------------------------------------------------------
    reg [PW-1:0] shift_p_re, shift_p_im;   // the pfifo write value
    (* shreg_extract = "no" *) reg [WIDTH-1:0] y0_r_re, y0_r_im;

    // L6 gates
    wire w_gate_ram = (k < TWO_D);
    wire w_gate_sd  = (k1 >= TWO_D && k1 < THREE_D);
    wire w_gate_dl  = (k1 >= THREE_D);
    wire w_gate_pf  = (k8 < TWO_D || k8 >= THREE_D);
    wire out_is_y0  = (k8 >= THREE_D);

    always @(posedge clk) begin
        if (rst) begin
            k      <= K_PRELOAD[KW-1:0];
            k1     <= K_PRELOAD[KW-1:0];
            k2     <= K_PRELOAD[KW-1:0];
            k3     <= K_PRELOAD[KW-1:0];
            k4     <= K_PRELOAD[KW-1:0];
            k5     <= K_PRELOAD[KW-1:0];
            k6     <= K_PRELOAD[KW-1:0];
            k7     <= K_PRELOAD[KW-1:0];
            k8     <= K_PRELOAD[KW-1:0];
            g_r    <= {DW{1'b0}};
            g_r2   <= {DW{1'b0}};
            a_r_re <= {BW{1'b0}}; a_r_im <= {BW{1'b0}};
            s_r_re <= {BW{1'b0}}; s_r_im <= {BW{1'b0}};
            d_r_re <= {BW{1'b0}}; d_r_im <= {BW{1'b0}};
            dl_r_re <= {BW{1'b0}}; dl_r_im <= {BW{1'b0}};
            x_r_re <= {BW{1'b0}}; x_r_im <= {BW{1'b0}};
            s0_r_re <= {BW{1'b0}}; s0_r_im <= {BW{1'b0}};
            sd_r_re <= {BW{1'b0}}; sd_r_im <= {BW{1'b0}};
            c1_r_re <= {CB{1'b0}}; c1_r_im <= {CB{1'b0}};
            c3_r_re <= {CB{1'b0}}; c3_r_im <= {CB{1'b0}};
            y0_raw_r_re <= {BW{1'b0}}; y0_raw_r_im <= {BW{1'b0}};
            y0_raw2_re <= {BW{1'b0}}; y0_raw2_im <= {BW{1'b0}};
            y0_raw3_re <= {BW{1'b0}}; y0_raw3_im <= {BW{1'b0}};
            y0_raw4_re <= {BW{1'b0}}; y0_raw4_im <= {BW{1'b0}};
            y0_raw5_re <= {BW{1'b0}}; y0_raw5_im <= {BW{1'b0}};
            prod_im_tr <= {MWB{1'b0}}; prod_im_ti <= {MWB{1'b0}};
            prod_re_tr <= {MWB{1'b0}}; prod_re_ti <= {MWB{1'b0}};
            c_ti <= {MWB{1'b0}}; c_tr <= {MWB{1'b0}};
            m_rh_re <= {CB{1'b0}};
            tr_rh <= {TWIDDLE_WIDTH{1'b0}}; ti_rh <= {TWIDDLE_WIDTH{1'b0}};
            m_r_re <= {CB{1'b0}}; m_r_im <= {CB{1'b0}};
            tr_r2 <= {TWIDDLE_WIDTH{1'b0}}; ti_r2 <= {TWIDDLE_WIDTH{1'b0}};
            p_re <= {MWB{1'b0}}; p_im <= {MWB{1'b0}};
            shift_p_re <= {PW{1'b0}}; shift_p_im <= {PW{1'b0}};
            y0_r_re <= {WIDTH{1'b0}}; y0_r_im <= {WIDTH{1'b0}};
            out_re <= {WIDTH{1'b0}};
            out_im <= {WIDTH{1'b0}};
        end else if (ce) begin
            // ---- L0 registers (the reads + the input of THIS clock) ----
            // (the data rings' reads are register-captured here; the
            // twiddle reads into the BRAM-style tw_dout)
            tw_dout <= tw_rom[rom_addr];
            a_r_re <= a0_re;    a_r_im <= a0_im;
            s_r_re <= s0_re;    s_r_im <= s0_im;
            d_r_re <= d0_re;    d_r_im <= d0_im;
            dl_r_re <= d1_re;   dl_r_im <= d1_im;
            x_r_re <= x_re;     x_r_im <= x_im;
            g_r    <= g_cur;

            // ---- L1 registers (the current L1 wires) ----
            s0_r_re <= s_x_re;  s0_r_im <= s_x_im;
            sd_r_re <= sd_re;   sd_r_im <= sd_im;
            c1_r_re <= c1_re;   c1_r_im <= c1_im;
            c3_r_re <= c3_re;   c3_r_im <= c3_im;
            y0_raw_r_re <= y0_raw_re;   y0_raw_r_im <= y0_raw_im;
            y0_raw2_re <= y0_raw_r_re;  y0_raw2_im <= y0_raw_r_im;
            y0_raw3_re <= y0_raw2_re;   y0_raw3_im <= y0_raw2_im;
            y0_raw4_re <= y0_raw3_re;   y0_raw4_im <= y0_raw3_im;
            y0_raw5_re <= y0_raw4_re;   y0_raw5_im <= y0_raw4_im;
            g_r2   <= g_r;
            k1 <= k; k2 <= k1; k3 <= k2; k4 <= k3;
            k5 <= k4; k6 <= k5; k7 <= k6; k8 <= k7;

            // ---- L2a: the DSP AREG/BREG operand capture ----------------
            m_r_re <= m_re;   m_r_im <= m_im;
            tr_r2 <= tr;      ti_r2 <= ti;

            // ---- L2b: the im-path products (MREG) + the frozen re-path
            // operands --------------------------------------------
            prod_im_ti <= m_r_im * ti_r2;
            prod_im_tr <= m_r_im * tr_r2;
            m_rh_re <= m_r_re;
            tr_rh   <= tr_r2;
            ti_rh   <= ti_r2;

            // ---- L3: the re-path products (MREG) + the im products at
            // the C ports (CREG) ---------------------------------
            prod_re_tr <= m_rh_re * tr_rh;
            prod_re_ti <= m_rh_re * ti_rh;
            c_ti <= prod_im_ti;
            c_tr <= prod_im_tr;

            // ---- L4: the re/im combine (the ALU/PREG) ------------
            p_re <= prod_re_tr - c_ti;
            p_im <= prod_re_ti + c_tr;

            // ---- L5: the round-half-up staging ------------------
            // PW == MWB + 1 by construction (CB = WIDTH+2), so the
            // combine result needs exactly one sign bit to reach the
            // shift staging width.
            if (k7 >= THREE_D) begin
                shift_p_re <= round_shift_pw({{1{p_re[MWB-1]}}, p_re}, TD_PLUS_S1);
                shift_p_im <= round_shift_pw({{1{p_im[MWB-1]}}, p_im}, TD_PLUS_S1);
            end else begin
                shift_p_re <= round_shift_pw({{1{p_re[MWB-1]}}, p_re}, TD_PLUS_BOTH);
                shift_p_im <= round_shift_pw({{1{p_im[MWB-1]}}, p_im}, TD_PLUS_BOTH);
            end
            y0_r_re <= round_shift_bw(y0_raw5_re, SIGMA1)[WIDTH-1:0];
            y0_r_im <= round_shift_bw(y0_raw5_im, SIGMA1)[WIDTH-1:0];

            // ---- L6: the writes and the output ------------------
            if (w_gate_ram) begin
                ram_re[rp] <= in_re;
                ram_im[rp] <= in_im;
            end
            if (w_gate_sd) begin
                sram_re[sp] <= s_x_re[WIDTH-1:0];
                sram_im[sp] <= s_x_im[WIDTH-1:0];
                dram_re[sp] <= d_x_re[WIDTH-1:0];
                dram_im[sp] <= d_x_im[WIDTH-1:0];
            end
            if (w_gate_dl) begin
                dline_re[sp] <= d_x_re[WIDTH-1:0];
                dline_im[sp] <= d_x_im[WIDTH-1:0];
            end
            if (w_gate_pf) begin
                pfifo_re[pwp] <= shift_p_re[WIDTH-1:0];
                pfifo_im[pwp] <= shift_p_im[WIDTH-1:0];
            end
            out_re <= out_is_y0 ? y0_r_re : pfifo_re[pr_r];
            out_im <= out_is_y0 ? y0_r_im : pfifo_im[pr_r];

            // ---- phase ----
            k    <= k + {{(KW-1){1'b0}}, 1'b1};
        end
    end

endmodule

`default_nettype wire
