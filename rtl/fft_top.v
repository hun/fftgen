// fftgen -- top binding wrapper: fft_sdf core + optional output reorder.
//
// REORDER_OUT=1 inserts the ping-pong bit-reversal reorder after the core
// (for native->native via DIF and bitrev->bitrev via DIT). All parameters
// pass through to the core; TOPOLOGY selects DIF (0) / DIT (1).

`default_nettype none

module fft_top #(
    parameter integer NUM_POINTS     = 16,
    parameter integer SAMPLE_WIDTH   = 16,
    parameter integer SAMPLE_DECIMAL = 0,
    parameter integer OUTPUT_WIDTH   = 16,
    parameter integer OUTPUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    parameter integer SCALING_PACK   = 32'h01010101,
    parameter integer TOPOLOGY       = 0,   // 0 = DIF, 1 = DIT
    parameter integer REORDER_OUT    = 0,   // 1 = bit-reverse the output
    parameter TWIDDLE_FILE           = "fft_twiddles.mem",
    parameter integer INTERN_WIDTH   = SAMPLE_WIDTH + 5,
    parameter integer PIPE_DEPTH     = 6
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
    output wire [NUM_POINTS*INTERN_WIDTH-1:0] dbg_stage_re,
    output wire [NUM_POINTS*INTERN_WIDTH-1:0] dbg_stage_im,

    input  wire                        rst
);

    generate
        if (REORDER_OUT != 0) begin : with_reorder
            wire core_valid;
            wire signed [INTERN_WIDTH-1:0] core_re, core_im;
            wire core_user, core_last;

            fft_sdf #(
                .NUM_POINTS     (NUM_POINTS),
                .SAMPLE_WIDTH   (SAMPLE_WIDTH),
                .SAMPLE_DECIMAL (SAMPLE_DECIMAL),
                .OUTPUT_WIDTH   (OUTPUT_WIDTH),
                .OUTPUT_DECIMAL (OUTPUT_DECIMAL),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .SCALING_PACK   (SCALING_PACK),
                .TOPOLOGY       (TOPOLOGY),
                .TWIDDLE_FILE   (TWIDDLE_FILE),
                .INTERN_WIDTH   (INTERN_WIDTH),
                .PIPE_DEPTH     (PIPE_DEPTH)
            ) u_core (
                .clk            (clk),
                .ce             (ce),
                .s_axis_tvalid  (s_axis_tvalid),
                .s_axis_tdata_re(s_axis_tdata_re),
                .s_axis_tdata_im(s_axis_tdata_im),
                .s_axis_tuser   (s_axis_tuser),
                .s_axis_tlast   (s_axis_tlast),
                .m_axis_tvalid  (core_valid),
                .m_axis_tdata_re(core_re),
                .m_axis_tdata_im(core_im),
                .m_axis_tuser   (core_user),
                .m_axis_tlast   (core_last),
                .rst            (rst)
            );

            fft_reorder #(
                .NUM_POINTS (NUM_POINTS),
                .WIDTH      (OUTPUT_WIDTH)
            ) u_reorder (
                .clk            (clk),
                .ce             (ce),
                .s_axis_tvalid  (core_valid),
                .s_axis_tdata_re(core_re),
                .s_axis_tdata_im(core_im),
                .s_axis_tuser   (core_user),
                .s_axis_tlast   (core_last),
                .m_axis_tvalid  (m_axis_tvalid),
                .m_axis_tdata_re(m_axis_tdata_re),
                .m_axis_tdata_im(m_axis_tdata_im),
                .m_axis_tuser   (m_axis_tuser),
                .m_axis_tlast   (m_axis_tlast),
                .rst            (rst)
            );
        end else begin : plain
            fft_sdf #(
                .NUM_POINTS     (NUM_POINTS),
                .SAMPLE_WIDTH   (SAMPLE_WIDTH),
                .SAMPLE_DECIMAL (SAMPLE_DECIMAL),
                .OUTPUT_WIDTH   (OUTPUT_WIDTH),
                .OUTPUT_DECIMAL (OUTPUT_DECIMAL),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .SCALING_PACK   (SCALING_PACK),
                .TOPOLOGY       (TOPOLOGY),
                .TWIDDLE_FILE   (TWIDDLE_FILE),
                .INTERN_WIDTH   (INTERN_WIDTH),
                .PIPE_DEPTH     (PIPE_DEPTH)
            ) u_core (
                .clk            (clk),
                .ce             (ce),
                .s_axis_tvalid  (s_axis_tvalid),
                .s_axis_tdata_re(s_axis_tdata_re),
                .s_axis_tdata_im(s_axis_tdata_im),
                .s_axis_tuser   (s_axis_tuser),
                .s_axis_tlast   (s_axis_tlast),
                .m_axis_tvalid  (m_axis_tvalid),
                .m_axis_tdata_re(m_axis_tdata_re),
                .m_axis_tdata_im(m_axis_tdata_im),
                .m_axis_tuser   (m_axis_tuser),
                .m_axis_tlast   (m_axis_tlast),
                .rst            (rst)
            );
        end
    endgenerate

endmodule

`default_nettype wire
