// fftgen -- top binding wrapper for R2^2 lane (M-point, DIF).
// Same AXI-stream framing contract as fft_top.v but the core is
// fft_sdf_r22 (P7). Used as the per-lane engine in SSR R=2 r22.

`default_nettype none
module fft_top_r22 #(
    parameter integer NUM_POINTS     = 8,
    parameter integer SAMPLE_WIDTH   = 16,
    parameter integer SAMPLE_DECIMAL = 0,
    parameter integer OUTPUT_WIDTH   = 16,
    parameter integer OUTPUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    parameter integer SCALING_PACK   = 32'h01010101,
    parameter integer TOPOLOGY       = 0,
    parameter integer REORDER_OUT    = 0,
    parameter TWIDDLE_FILE           = "fft_twiddles_r22_lane.mem",
    parameter integer INTERN_WIDTH   = SAMPLE_WIDTH + 5,
    parameter integer PIPE_DEPTH     = 10,
    parameter integer TWIDDLE_MEM   = 0
)(
    input  wire                        clk,
    input  wire                        ce,
    input  wire                        s_axis_tvalid,
    input  wire [SAMPLE_WIDTH-1:0]     s_axis_tdata_re,
    input  wire [SAMPLE_WIDTH-1:0]     s_axis_tdata_im,
    input  wire                        s_axis_tuser,
    input  wire                        s_axis_tlast,
    output wire                        m_axis_tvalid,
    output wire [OUTPUT_WIDTH-1:0]     m_axis_tdata_re,
    output wire [OUTPUT_WIDTH-1:0]     m_axis_tdata_im,
    output wire                        m_axis_tuser,
    output wire                        m_axis_tlast,
    input  wire                        rst
);
    generate
        if (REORDER_OUT != 0) begin : with_reorder
            wire core_valid;
            wire signed [INTERN_WIDTH-1:0] core_re, core_im;
            wire core_user, core_last;
            fft_sdf_r22 #(
                .NUM_POINTS(NUM_POINTS), .SAMPLE_WIDTH(SAMPLE_WIDTH),
                .SAMPLE_DECIMAL(SAMPLE_DECIMAL), .OUTPUT_WIDTH(OUTPUT_WIDTH),
                .OUTPUT_DECIMAL(OUTPUT_DECIMAL), .TWIDDLE_WIDTH(TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL), .SCALING_PACK(SCALING_PACK),
                .TOPOLOGY(TOPOLOGY), .TWIDDLE_FILE(TWIDDLE_FILE),
                .INTERN_WIDTH(INTERN_WIDTH), .PIPE_DEPTH(PIPE_DEPTH),
                .TWIDDLE_MEM(TWIDDLE_MEM)
            ) u_core (
                .clk(clk), .ce(ce), .s_axis_tvalid(s_axis_tvalid),
                .s_axis_tdata_re(s_axis_tdata_re), .s_axis_tdata_im(s_axis_tdata_im),
                .s_axis_tuser(s_axis_tuser), .s_axis_tlast(s_axis_tlast),
                .m_axis_tvalid(core_valid), .m_axis_tdata_re(core_re), .m_axis_tdata_im(core_im),
                .m_axis_tuser(core_user), .m_axis_tlast(core_last), .rst(rst)
            );
            fft_reorder #(.NUM_POINTS(NUM_POINTS), .WIDTH(OUTPUT_WIDTH)) u_reorder (
                .clk(clk), .ce(ce),
                .s_axis_tvalid(core_valid), .s_axis_tdata_re(core_re), .s_axis_tdata_im(core_im),
                .s_axis_tuser(core_user), .s_axis_tlast(core_last),
                .m_axis_tvalid(m_axis_tvalid), .m_axis_tdata_re(m_axis_tdata_re), .m_axis_tdata_im(m_axis_tdata_im),
                .m_axis_tuser(m_axis_tuser), .m_axis_tlast(m_axis_tlast), .rst(rst)
            );
        end else begin : plain
            fft_sdf_r22 #(
                .NUM_POINTS(NUM_POINTS), .SAMPLE_WIDTH(SAMPLE_WIDTH),
                .SAMPLE_DECIMAL(SAMPLE_DECIMAL), .OUTPUT_WIDTH(OUTPUT_WIDTH),
                .OUTPUT_DECIMAL(OUTPUT_DECIMAL), .TWIDDLE_WIDTH(TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL), .SCALING_PACK(SCALING_PACK),
                .TOPOLOGY(TOPOLOGY), .TWIDDLE_FILE(TWIDDLE_FILE),
                .INTERN_WIDTH(INTERN_WIDTH), .PIPE_DEPTH(PIPE_DEPTH),
                .TWIDDLE_MEM(TWIDDLE_MEM)
            ) u_core (
                .clk(clk), .ce(ce), .s_axis_tvalid(s_axis_tvalid),
                .s_axis_tdata_re(s_axis_tdata_re), .s_axis_tdata_im(s_axis_tdata_im),
                .s_axis_tuser(s_axis_tuser), .s_axis_tlast(s_axis_tlast),
                .m_axis_tvalid(m_axis_tvalid), .m_axis_tdata_re(m_axis_tdata_re), .m_axis_tdata_im(m_axis_tdata_im),
                .m_axis_tuser(m_axis_tuser), .m_axis_tlast(m_axis_tlast), .rst(rst)
            );
        end
    endgenerate
endmodule
`default_nettype wire
