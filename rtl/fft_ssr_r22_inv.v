// fftgen -- SSR R=2 R2^2 IFFT top, corner order bitreversed -> native (P8 4b).
// The TRANSPOSE of the forward network (doc/plan_p8_ssr_orders.md 3b): the
// R-point inverse runs FIRST, the per-lane engines LAST -- so no r22 DIT
// lane is needed.
//
// Contract (the flat corner conventions, mirroring SSRCornerInverseModel):
//   slot e = (2c+q) IN  carries the bin  X[bitrev_N(e)] = X[q*M + bitrev_M(c)]
//   slot e = (2c+q) OUT carries the time sample x[e] = x[2c+q]   (flat native)
// Concatenating the P8 forward FFT (fft_ssr_r22, REORDER_OUT=0) with this
// core is therefore the identity on x -- the TX/RX fast-convolution pair.
//
// Per clock (word = lanes 0,1):
//   a0 = round_shift(x0 + x1, 1)              -> lane 0 (twiddle W^0 = 1)
//   a1 = round_shift(x0 - x1, 1) * conj(W_N^p)-> lane 1
// with p = bitrev_M(counter): the word counter names the ARRIVAL slot, and
// the slot carries bin bitrev_M(counter) -- the same convention fft_cross
// uses for its EMIT_BREV WN row, so the two cores agree on bin labels.
//
// Fixed-point contract (mirrors SSRCornerInverseModel.tick exactly):
//   * the add/sub is exact at SW+2 bits (one guard bit)
//   * the fused >>1 (s_x = log2 R = 1) is round-half-up
//   * a1 is KEPT at SW+1 bits through the multiply: round_shift of the full
//     difference can reach exactly 2^(SW-1), which does not fit SW bits
//     (wrapping it would corrupt the boundary bin at x0=+max, x1=-max)
//   * the post-twiddle product is kept EXACT (PW = SW+1+TW bits; the +/-
//     combine fits with one guard bit, CW = PW+1)
//   * lane 1 is quantized EXACTLY ONCE to sample_width (round-half-up at
//     twiddle_decimal, then saturate) -- the lane-input contract
//   * a0 needs no requantise: the halved sum of two in-range samples fits
//     sample_width exactly
//
// Each lane then: fft_reorder (bitrev_M arrival -> native, the per-lane
// input reorder) feeding the EXISTING verified M-point DIF-IDFT lane
// (fft_top_r22, TOPOLOGY=0, INVERSE=1, REORDER_OUT=1, native-in
// native-out). Lane q emits x[2c+q] at native slot c.
//
// PIPELINE (depth 4 -- the twiddle MUST pair with the add/sub's word):
//   S0: q <= din(c); wa <= rom[bitrev_M(c)]      (word c's twiddle)
//   S1: a0/a1 <= rshift(q sums)  [word c-1];  wq <= wa  [word c-1's twiddle]
//   S2: t <= exact complex product  [word c-1]  (registered: the DSP
//       PREG/ALU boundary -- without it the round+sat share the multiply's
//       cycle and the path A/B-reg -> MULT -> ALU -> fabric round+sat ->
//       r1 measures ~4 ns, the -2.2 ns violation of the first N=2048
//       synth; the same pp/b register split fft_cross uses)
//   S3: r1 <= quant(t) [round-half-up at TD + saturate];  r0 <= a0
// Reading the ROM at S1 instead pairs word c's twiddle with word c-1's
// data -- a one-word bin mislabel (the exact bug class that sank the first
// bring-up). The S0 read + S1 coefficient hop keeps both in lockstep.
//
// FRAME ALIGNMENT (the trap that sank the first bring-up): the wrapper's
// pipeline-fill cycles present reset garbage on r0/r1. If the input
// reorders counted those cycles, their ping-pong frame boundary would
// shift by the wrapper depth and the emitted frames would be rotated.
// The reorders are therefore gated by s_axis_tvalid delayed by exactly
// the wrapper depth, so reorder cycle 0 is word 0. Data and markers ride
// the same delay, so they cannot drift apart.
//
// R > 2 is not expressible this way: bitrev_R is a real permutation and
// not affine mod R. SSRCornerInverseModel / config.SSR_CORNER_ORDERS guard
// it in the generator.

`default_nettype none
module fft_ssr_r22_inv #(
    parameter integer NUM_POINTS      = 16,
    parameter integer SSR             = 2,
    parameter integer SAMPLE_WIDTH    = 16,
    parameter integer SAMPLE_DECIMAL  = 0,
    parameter integer OUTPUT_WIDTH    = 16,
    parameter integer OUTPUT_DECIMAL  = 0,
    parameter integer TWIDDLE_WIDTH   = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    parameter integer SCALING_PACK    = 32'h01010101,
    parameter integer INTERN_WIDTH    = SAMPLE_WIDTH + 5,
    parameter integer PIPE_DEPTH      = 10,
    parameter         W1_FILE         = "fft_w1_inv.mem",   // W_N^{-p}, p in [0,M)
    parameter         LANE_TW_FILE    = "fft_twiddles_r22_lane.mem",
    parameter integer INVERSE         = 1
)(
    input  wire                         clk,
    input  wire                         ce,
    input  wire                         s_axis_tvalid,
    input  wire [SAMPLE_WIDTH*SSR-1:0]  s_axis_tdata_re,
    input  wire [SAMPLE_WIDTH*SSR-1:0]  s_axis_tdata_im,
    input  wire                         s_axis_tuser,
    input  wire                         s_axis_tlast,
    output wire                         m_axis_tvalid,
    output wire [OUTPUT_WIDTH*SSR-1:0]  m_axis_tdata_re,
    output wire [OUTPUT_WIDTH*SSR-1:0]  m_axis_tdata_im,
    output wire                         m_axis_tuser,
    output wire                         m_axis_tlast,
    input  wire                         rst
);
    localparam integer SW = SAMPLE_WIDTH;
    localparam integer OW = OUTPUT_WIDTH;
    localparam integer TW = TWIDDLE_WIDTH;
    localparam integer TD = TWIDDLE_DECIMAL;
    localparam integer M  = NUM_POINTS / SSR;          // SSR is 2
    localparam integer MW = (M > 1) ? $clog2(M) : 1;
    // a1 is SW+1 bits; product SW+1+TW; the +/- combine fits with one more
    localparam integer PW = SW + 1 + TW;
    localparam integer CW = PW + 1;

    wire run = ce && s_axis_tvalid;

    // ---- stage 0: input registers + word counter + twiddle fetch -----
    // cq tracks the word entering q; the S0 ROM read uses cq's CURRENT
    // value (word c), and the S1 add/sub reads q (word c-1) -- the
    // coefficient hop wq <= wa then re-pairs them (see header).
    reg signed [SW-1:0] q0_re, q0_im, q1_re, q1_im;
    reg [MW-1:0] cq;

    // bitrev_M(cq) -> the bin index the arriving word carries
    wire [MW-1:0] p;
    genvar gb;
    generate
        for (gb = 0; gb < MW; gb = gb + 1) begin : p_bitrev
            assign p[gb] = cq[MW-1-gb];
        end
    endgenerate

    // twiddle ROM: word p = W_N^{-p} (quantized, inverse-conjugated);
    // distributed like fft_cross's WN ROM (async read, registered here)
    (* ram_style = "distributed" *)
    reg signed [TW*2-1:0] w1_rom [0:M-1];
    initial begin
        $readmemh(W1_FILE, w1_rom);
    end

    // ---- stage 1: coefficient hop + a0/a1 ----------------------------
    reg signed [TW-1:0] wa_re, wa_im;    // word c's twiddle (S0)
    reg signed [TW-1:0] wq_re, wq_im;    // word c-1's twiddle (S1)
    reg signed [SW:0]   a0_re, a0_im, a1_re, a1_im;

    // ---- stage 2: partial products (registered at the DSP MREG) -----
    reg signed [PW-1:0] m_rr_r, m_ii_r, m_ri_r, m_ir_r;
    // ---- stage 3: fabric combine + a0 carries -----------------------
    reg signed [CW-1:0] t_re_r, t_im_r;
    reg signed [SW-1:0] a0_re_r, a0_im_r;   // a0 first carry (S2)
    reg signed [SW-1:0] a0_rr_re, a0_rr_im; // a0 second carry (S3)
    // ---- stage 4: lane-0 carry + lane-1 quantized product -----------
    reg signed [SW-1:0] r0_re, r0_im, r1_re, r1_im;
    // markers and tvalid ride the same 5-stage wrapper pipeline
    reg [4:0] mk_u, mk_l, tv;

    // round-half-up >>1 on an (SW+1)-bit signed value; the add runs at
    // SW+2 bits because |x0 +- x1| + 1 can reach 2^SW, overflowing SW+1.
    // $signed() wrapper is load-bearing: without it the +1 turns the whole
    // expression unsigned and >>> becomes a LOGICAL shift (the P8 probe
    // trap -- see doc/lessons_debugging.md).
    function signed [SW:0] rshift1;
        input signed [SW+1:0] v;
        reg signed [SW+1:0]   t;
        begin
            t = v + ($signed({{(SW+1){1'b0}}, 1'b1}));
            rshift1 = t >>> 1;
        end
    endfunction

    // exact complex product on explicitly sized wires (PW bits each; the
    // +/- combine fits in CW). Kept as wires so synthesis maps each
    // partial to a DSP operand port and the probe can read them.
    wire signed [PW-1:0] m_rr = $signed(a1_re) * $signed(wq_re);  // a1r*wr
    wire signed [PW-1:0] m_ii = $signed(a1_im) * $signed(wq_im);  // a1i*wi
    wire signed [PW-1:0] m_ri = $signed(a1_re) * $signed(wq_im);  // a1r*wi
    wire signed [PW-1:0] m_ir = $signed(a1_im) * $signed(wq_re);  // a1i*wr
    wire signed [CW-1:0] t_re = $signed(m_rr) - $signed(m_ii);
    wire signed [CW-1:0] t_im = $signed(m_ri) + $signed(m_ir);

    // round-half-up >>>TD then saturate to SW bits -- exactly
    // quantize_output(t, sd+td, sw, sd) from the golden model
    function signed [SW-1:0] quant_sat;
        input signed [CW-1:0] v;
        reg signed [CW-1:0]   r;
        begin
            if (TD > 0)
                r = (v + ($signed({{(CW-1){1'b0}}, 1'b1}) <<< (TD - 1))) >>> TD;
            else
                r = v;
            if (r[CW-1:SW] == {{(CW-SW){r[SW-1]}}})
                quant_sat = r[SW-1:0];               // in range
            else if (!r[CW-1])
                quant_sat = {1'b0, {(SW-1){1'b1}}};  // + saturate
            else
                quant_sat = {1'b1, {(SW-1){1'b0}}};  // - saturate
        end
    endfunction

    always @(posedge clk) begin
        if (rst) begin
            q0_re <= {SW{1'b0}}; q0_im <= {SW{1'b0}};
            q1_re <= {SW{1'b0}}; q1_im <= {SW{1'b0}};
            cq    <= {MW{1'b0}};
            wa_re <= {TW{1'b0}}; wa_im <= {TW{1'b0}};
            wq_re <= {TW{1'b0}}; wq_im <= {TW{1'b0}};
            a0_re <= {SW+1{1'b0}}; a0_im <= {SW+1{1'b0}};
            a1_re <= {SW+1{1'b0}}; a1_im <= {SW+1{1'b0}};
            r0_re <= {SW{1'b0}}; r0_im <= {SW{1'b0}};
            r1_re <= {SW{1'b0}}; r1_im <= {SW{1'b0}};
            t_re_r <= {CW{1'b0}}; t_im_r <= {CW{1'b0}};
            a0_re_r <= {SW{1'b0}}; a0_im_r <= {SW{1'b0}};
            a0_rr_re <= {SW{1'b0}}; a0_rr_im <= {SW{1'b0}};
            m_rr_r <= {PW{1'b0}}; m_ii_r <= {PW{1'b0}};
            m_ri_r <= {PW{1'b0}}; m_ir_r <= {PW{1'b0}};
            mk_u  <= 5'b00000; mk_l <= 5'b00000; tv <= 5'b00000;
        end else if (run) begin
            // stage 0: input registers, word counter, twiddle fetch
            q0_re <= s_axis_tdata_re[0 +: SW];
            q0_im <= s_axis_tdata_im[0 +: SW];
            q1_re <= s_axis_tdata_re[SW +: SW];
            q1_im <= s_axis_tdata_im[SW +: SW];
            cq    <= cq + {{(MW-1){1'b0}}, 1'b1};
            wa_re <= w1_rom[p][TW*2-1:TW];
            wa_im <= w1_rom[p][TW-1:0];
            mk_u[0] <= s_axis_tuser;
            mk_l[0] <= s_axis_tlast;
            tv[0]   <= 1'b1;
            // stage 1: add/sub (word c-1 from q) + coefficient hop
            a0_re <= rshift1({{2{q0_re[SW-1]}}, q0_re} + {{2{q1_re[SW-1]}}, q1_re});
            a0_im <= rshift1({{2{q0_im[SW-1]}}, q0_im} + {{2{q1_im[SW-1]}}, q1_im});
            a1_re <= rshift1({{2{q0_re[SW-1]}}, q0_re} - {{2{q1_re[SW-1]}}, q1_re});
            a1_im <= rshift1({{2{q0_im[SW-1]}}, q0_im} - {{2{q1_im[SW-1]}}, q1_im});
            wq_re <= wa_re;
            wq_im <= wa_im;
            mk_u[1] <= mk_u[0]; mk_l[1] <= mk_l[0]; tv[1] <= tv[0];
            // stage 2: partial products, registered at the DSP MREG --
            // the fabric combine then starts from MREG, not from the
            // A/B regs through the DSP ALU (the -0.89 ns path)
            m_rr_r <= $signed(a1_re) * $signed(wq_re);
            m_ii_r <= $signed(a1_im) * $signed(wq_im);
            m_ri_r <= $signed(a1_re) * $signed(wq_im);
            m_ir_r <= $signed(a1_im) * $signed(wq_re);
            a0_re_r <= a0_re[SW-1:0];
            a0_im_r <= a0_im[SW-1:0];
            mk_u[2] <= mk_u[1]; mk_l[2] <= mk_l[1]; tv[2] <= tv[1];
            // stage 3: fabric combine (exact); a0 rides a0_re_r
            t_re_r <= $signed(m_rr_r) - $signed(m_ii_r);
            t_im_r <= $signed(m_ri_r) + $signed(m_ir_r);
            a0_rr_re <= a0_re_r;
            a0_rr_im <= a0_im_r;
            mk_u[3] <= mk_u[2]; mk_l[3] <= mk_l[2]; tv[3] <= tv[2];
            // stage 4: lane 0 carries a0 (fits SW bits exactly); lane 1
            // gets one quantize to sample_width
            r0_re <= a0_rr_re;
            r0_im <= a0_rr_im;
            r1_re <= quant_sat(t_re_r);
            r1_im <= quant_sat(t_im_r);
            mk_u[4] <= mk_u[3]; mk_l[4] <= mk_l[3]; tv[4] <= tv[3];
        end
    end

    // the reorders count cycles from the FIRST REAL WORD (wrapper filled);
    // gating by the delayed tvalid keeps their ping-pong frame boundary on
    // the data's actual frame boundary (see header note)
    wire re_tvalid = tv[4];

    genvar r;
    generate
        for (r = 0; r < 2; r = r + 1) begin : lanes
            wire                       v;
            wire [SW-1:0]              dre, dim;
            wire [OW-1:0]              ore, oim;
            wire                       ou, ol;
            wire [SW-1:0]              ird, iid;   // input-reorder output
            wire                       irv, iru, irl;
            assign dre = (r == 0) ? r0_re : r1_re;
            assign dim = (r == 0) ? r0_im : r1_im;
            // per-lane input reorder: bitrev_M arrival -> native order
            fft_reorder #(.NUM_POINTS(M), .WIDTH(SW)) u_ireord (
                .clk(clk), .ce(ce),
                .s_axis_tvalid(re_tvalid),
                .s_axis_tdata_re(dre), .s_axis_tdata_im(dim),
                .s_axis_tuser((r == 0) ? mk_u[4] : 1'b0),
                .s_axis_tlast((r == 1) ? mk_l[4] : 1'b0),
                .m_axis_tvalid(irv), .m_axis_tdata_re(ird), .m_axis_tdata_im(iid),
                .m_axis_tuser(iru), .m_axis_tlast(irl), .rst(rst)
            );
            // existing verified M-point DIF-IDFT lane (native-in native-out)
            fft_top_r22 #(
                .NUM_POINTS(M), .SAMPLE_WIDTH(SW), .SAMPLE_DECIMAL(SAMPLE_DECIMAL),
                .OUTPUT_WIDTH(OW), .OUTPUT_DECIMAL(OUTPUT_DECIMAL),
                .TWIDDLE_WIDTH(TW), .TWIDDLE_DECIMAL(TD),
                .SCALING_PACK(SCALING_PACK), .TOPOLOGY(0), .INVERSE(INVERSE),
                .REORDER_OUT(1),
                .TWIDDLE_FILE(LANE_TW_FILE),
                .INTERN_WIDTH(INTERN_WIDTH), .PIPE_DEPTH(PIPE_DEPTH)
            ) u_lane (
                .clk(clk), .ce(ce), .rst(rst),
                .s_axis_tvalid(irv),
                .s_axis_tdata_re(ird), .s_axis_tdata_im(iid),
                .s_axis_tuser(iru), .s_axis_tlast(irl),
                .m_axis_tvalid(v), .m_axis_tdata_re(ore), .m_axis_tdata_im(oim),
                .m_axis_tuser(ou), .m_axis_tlast(ol)
            );
            if (r == 0) begin : g_o0
                assign m_axis_tvalid = v;
                assign m_axis_tuser  = ou;
                assign m_axis_tdata_re[0 +: OW]   = ore;
                assign m_axis_tdata_im[0 +: OW]   = oim;
            end else begin : g_o1
                assign m_axis_tlast  = ol;
                assign m_axis_tdata_re[OW +: OW]  = ore;
                assign m_axis_tdata_im[OW +: OW]  = oim;
            end
        end
    endgenerate
endmodule
`default_nettype wire
