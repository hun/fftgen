// fftgen -- SSR R=2 R2^2 top: R lanes x M-point R2^2 engines + crossbar.
// Mirrors fft_ssr.v but lanes are fft_top_r22 (M-point, DIF, REORDER_OUT=1).
// N = R * M, R=2 for this first bring-up (P7). Crossbar (fft_cross) is
// unchanged -- pre-twiddle W_N^{r*p} + R-point DFT.
//
// P8 adds REORDER_OUT (default 1 = the native -> native contract above).
// REORDER_OUT=0 gives the corner order native -> bitreversed at R=2: the DIF
// lanes then emit A_r[p] in bit-reversed p order, which IS the bitrev_N
// emission because bitrev_2 is the identity, and fft_cross is told to name
// its WN row by bitrev(counter). One parameter drives BOTH halves on purpose
// -- they must agree or the bins are silently mislabelled (doc/plan_p8_ssr_orders.md).
// It also removes the per-lane reorder buffers (2M x 2W words) and M clocks
// of lane latency, so the core's total latency is a CONTRACT CHANGE.
// R > 2 with REORDER_OUT=0 is not expressible this way: bitrev_R is then a
// real permutation and is not affine mod R, so no wiring absorbs it.

`default_nettype none
module fft_ssr_r22 #(
    parameter integer NUM_POINTS     = 16,
    parameter integer SSR            = 2,
    parameter integer SAMPLE_WIDTH   = 16,
    parameter integer SAMPLE_DECIMAL = 0,
    parameter integer OUTPUT_WIDTH   = 16,
    parameter integer OUTPUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    parameter integer SCALING_PACK   = 32'h01010101,
    parameter integer INTERN_WIDTH   = SAMPLE_WIDTH + 5,
    parameter integer PIPE_DEPTH     = 10,
    parameter         WN_FILE        = "fft_wn.mem",
    parameter         LANE_TW_FILE   = "fft_twiddles_r22_lane.mem",
    parameter integer REORDER_OUT    = 1,     // P8: 0 = native -> bitrev (R=2)
    parameter         INVERSE        = 0
)(
    input  wire                     clk,
    input  wire                     ce,
    input  wire                     s_axis_tvalid,
    input  wire [SAMPLE_WIDTH*SSR-1:0] s_axis_tdata_re,
    input  wire [SAMPLE_WIDTH*SSR-1:0] s_axis_tdata_im,
    input  wire                     s_axis_tuser,
    input  wire                     s_axis_tlast,
    output wire                     m_axis_tvalid,
    output wire [OUTPUT_WIDTH*SSR-1:0] m_axis_tdata_re,
    output wire [OUTPUT_WIDTH*SSR-1:0] m_axis_tdata_im,
    output wire                     m_axis_tuser,
    output wire                     m_axis_tlast,
    input  wire                     rst
);
    localparam integer R = SSR;
    localparam integer M = NUM_POINTS / R;

    genvar r;
    generate
        for (r = 0; r < R; r = r+1) begin : lanes
            wire                       v;
            wire [SAMPLE_WIDTH-1:0]    dre, dim;
            wire [OUTPUT_WIDTH-1:0]    ore, oim;
            wire                       ou, ol;
            assign dre = s_axis_tdata_re[r*SAMPLE_WIDTH +: SAMPLE_WIDTH];
            assign dim = s_axis_tdata_im[r*SAMPLE_WIDTH +: SAMPLE_WIDTH];
            fft_top_r22 #(
                .NUM_POINTS(M), .SAMPLE_WIDTH(SAMPLE_WIDTH), .SAMPLE_DECIMAL(SAMPLE_DECIMAL),
                .OUTPUT_WIDTH(OUTPUT_WIDTH), .OUTPUT_DECIMAL(OUTPUT_DECIMAL),
                .TWIDDLE_WIDTH(TWIDDLE_WIDTH), .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .SCALING_PACK(SCALING_PACK), .TOPOLOGY(0), .INVERSE(INVERSE),
                .REORDER_OUT(REORDER_OUT),
                .TWIDDLE_FILE(LANE_TW_FILE),
                .INTERN_WIDTH(INTERN_WIDTH), .PIPE_DEPTH(PIPE_DEPTH)
            ) u_lane (
                .clk(clk), .ce(ce), .rst(rst),
                .s_axis_tvalid(s_axis_tvalid),
                .s_axis_tdata_re(dre), .s_axis_tdata_im(dim),
                .s_axis_tuser(s_axis_tuser && (r==0)), .s_axis_tlast(s_axis_tlast && (r==R-1)),
                .m_axis_tvalid(v), .m_axis_tdata_re(ore), .m_axis_tdata_im(oim),
                .m_axis_tuser(ou), .m_axis_tlast(ol)
            );
        end
    endgenerate

    wire signed [R*OUTPUT_WIDTH-1:0] xb_re, xb_im;
    genvar g;
    generate for (g=0; g<R; g=g+1) begin : gather
        assign xb_re[g*OUTPUT_WIDTH +: OUTPUT_WIDTH] = $signed(lanes[g].ore);
        assign xb_im[g*OUTPUT_WIDTH +: OUTPUT_WIDTH] = $signed(lanes[g].oim);
    end endgenerate

    wire                     xb_valid, xb_user, xb_last;
    wire signed [R*OUTPUT_WIDTH-1:0] xb_ore, xb_oim;
    fft_cross #(
        .NUM_POINTS(NUM_POINTS), .SSR(R), .OUT_WIDTH(OUTPUT_WIDTH), .OUT_DECIMAL(OUTPUT_DECIMAL),
        .TWIDDLE_WIDTH(TWIDDLE_WIDTH), .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
        .WN_FILE(WN_FILE), .INVERSE(INVERSE),
        .EMIT_BREV(REORDER_OUT ? 0 : 1)
    ) u_cross (
        .clk(clk), .ce(ce), .rst(rst),
        .in_valid(lanes[0].v),
        // markers enter WITH their data word and are delayed by the
        // datapath's own depth inside fft_cross (P8). They used to be
        // re-timed here by a hard-wired 3-tap pipe, which lagged every
        // crossbar stage added since the input register (CB_LAT is 7/11):
        // tuser/tlast emerged 4 clocks (8 at R=8) early.
        .in_user(lanes[0].ou), .in_last(lanes[R-1].ol),
        .din_re(xb_re), .din_im(xb_im),
        .out_valid(xb_valid), .out_user(xb_user), .out_last(xb_last),
        .dout_re(xb_ore), .dout_im(xb_oim)
    );

    assign m_axis_tvalid = xb_valid;
    assign m_axis_tuser  = xb_user;
    assign m_axis_tlast  = xb_last;
    genvar q;
    generate for (q=0; q<R; q=q+1) begin : outpack
        assign m_axis_tdata_re[q*OUTPUT_WIDTH +: OUTPUT_WIDTH] = xb_ore[q*OUTPUT_WIDTH +: OUTPUT_WIDTH];
        assign m_axis_tdata_im[q*OUTPUT_WIDTH +: OUTPUT_WIDTH] = xb_oim[q*OUTPUT_WIDTH +: OUTPUT_WIDTH];
    end endgenerate
endmodule
`default_nettype wire
