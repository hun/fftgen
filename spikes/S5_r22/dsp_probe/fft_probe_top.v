// P7 step 7 timing probe: one R2^2 pair stage, OOC.
// D=128 (the pairs[0] shape of N=512; the intra-DSP path is
// size-independent per the r2 sweep notes).
`default_nettype none

module fft_probe_top #(
    parameter integer DEPTH = 128
)(
    input  wire clk,
    input  wire rst,
    input  wire signed [15:0] in_re,
    input  wire signed [15:0] in_im,
    output wire signed [15:0] out_re,
    output wire signed [15:0] out_im
);

    fft_stage_r22 #(
        .DEPTH(DEPTH),
        .WIDTH(16),
        .SIGMA0(1),
        .SIGMA1(1),
        .TWIDDLE_WIDTH(18),
        .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0),
        .NPTS(4 * DEPTH),
        .INVERSE(0),
        .K_PRELOAD(16'h0),
        .TWIDDLE_FILE("fft_twiddles_r22.mem")
    ) u_stage (
        .clk(clk), .ce(1'b1), .rst(rst),
        .in_re(in_re), .in_im(in_im),
        .out_re(out_re), .out_im(out_im)
    );

endmodule

`default_nettype wire
