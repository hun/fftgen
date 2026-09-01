// S7 full-core probe: the complete fft_sdf_r23 (3 r23 triples + 2 r22
// leftover pairs, N=8192) OOC on KU5P @ 2 ns.
`default_nettype none

module fft_probe_core_top (
    input  wire        clk,
    input  wire        rst,
    input  wire        s_axis_tvalid,
    input  wire [15:0] s_axis_tdata_re,
    input  wire [15:0] s_axis_tdata_im,
    input  wire        s_axis_tuser,
    input  wire        s_axis_tlast,
    output wire        m_axis_tvalid,
    output wire [15:0] m_axis_tdata_re,
    output wire [15:0] m_axis_tdata_im,
    output wire        m_axis_tuser,
    output wire        m_axis_tlast
);

    fft_sdf_r23 #(
        .NUM_POINTS(8192),
        .SAMPLE_WIDTH(16),
        .TWIDDLE_WIDTH(18),
        .TWIDDLE_DECIMAL(17),
        .SCALING_PACK(32'h01555555),
        .INVERSE(0),
        .INTERN_WIDTH(16),
        .TWIDDLE_FILE_T0("fft_tw_r23_t0.mem"),
        .TWIDDLE_FILE_T1("fft_tw_r23_t1.mem"),
        .TWIDDLE_FILE_T2("fft_tw_r23_t2.mem"),
        .TWIDDLE_FILE_L0("fft_tw_r22_l0.mem"),
        .TWIDDLE_FILE_L1("fft_tw_r22_l1.mem")
    ) u_core (
        .clk(clk), .ce(1'b1), .rst(rst),
        .s_axis_tvalid(s_axis_tvalid),
        .s_axis_tdata_re(s_axis_tdata_re),
        .s_axis_tdata_im(s_axis_tdata_im),
        .s_axis_tuser(s_axis_tuser),
        .s_axis_tlast(s_axis_tlast),
        .m_axis_tvalid(m_axis_tvalid),
        .m_axis_tdata_re(m_axis_tdata_re),
        .m_axis_tdata_im(m_axis_tdata_im),
        .m_axis_tuser(m_axis_tuser),
        .m_axis_tlast(m_axis_tlast)
    );

endmodule

`default_nettype wire
