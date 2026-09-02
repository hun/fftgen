// fftgen -- SSR R=2 R2^3 top (S7): R lanes x M-point R2^3 engines + crossbar.
// Mirrors fft_ssr_r22.v but the lanes are fft_sdf_r23 (M-point, DIF,
// natively bit-reversed output) and the crossbar runs the P8 corner order
// (EMIT_BREV=1): the r23 lane's own output order IS the bitrev_M emission
// the crossbar needs at R=2 (bitrev_2 = identity), so -- unlike the r22b
// variant -- the corner order costs NO lane reorder buffers and no extra
// latency. The core's output stream is bit-reversed bins (native -> bitrev
// contract); R > 2 is not expressible this way (bitrev_R would be a real
// permutation; doc/plan_p8_ssr_orders.md).

`default_nettype none
module fft_ssr_r23 #(
    parameter integer NUM_POINTS     = 16,
    parameter integer SSR            = 2,
    parameter integer SAMPLE_WIDTH   = 16,
    parameter integer SAMPLE_DECIMAL = 0,
    parameter integer OUTPUT_WIDTH   = 16,
    parameter integer OUTPUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    parameter integer SCALING_PACK   = 32'h01555555,
    parameter integer INTERN_WIDTH   = 16,
    parameter integer USE_URAM       = 0,
    parameter         WN_FILE        = "fft_wn.mem",
    parameter         LANE_TW_T0     = "fft_tw_r23_t0.mem",
    parameter         LANE_TW_T1     = "fft_tw_r23_t1.mem",
    parameter         LANE_TW_T2     = "fft_tw_r23_t2.mem",
    parameter         LANE_TW_L      = "fft_tw_r22_l.mem",
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
            fft_sdf_r23 #(
                .NUM_POINTS(M), .SAMPLE_WIDTH(SAMPLE_WIDTH),
                .SAMPLE_DECIMAL(SAMPLE_DECIMAL),
                .OUTPUT_WIDTH(OUTPUT_WIDTH), .OUTPUT_DECIMAL(OUTPUT_DECIMAL),
                .TWIDDLE_WIDTH(TWIDDLE_WIDTH), .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .SCALING_PACK(SCALING_PACK), .INVERSE(INVERSE),
                .INTERN_WIDTH(INTERN_WIDTH), .USE_URAM(USE_URAM),
                .TWIDDLE_FILE_T0(LANE_TW_T0), .TWIDDLE_FILE_T1(LANE_TW_T1),
                .TWIDDLE_FILE_T2(LANE_TW_T2), .TWIDDLE_FILE_L(LANE_TW_L)
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
    // the r23 lanes emit A_r[p] in bit-reversed p order natively: the
    // corner order -- EMIT_BREV=1 (the crossbar names its WN row by
    // bitrev_M(counter)); output stream = bit-reversed bins
    fft_cross #(
        .NUM_POINTS(NUM_POINTS), .SSR(R), .OUT_WIDTH(OUTPUT_WIDTH), .OUT_DECIMAL(OUTPUT_DECIMAL),
        .TWIDDLE_WIDTH(TWIDDLE_WIDTH), .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
        .WN_FILE(WN_FILE), .INVERSE(INVERSE),
        .EMIT_BREV(1)
    ) u_cross (
        .clk(clk), .ce(ce), .rst(rst),
        .in_valid(lanes[0].v),
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
