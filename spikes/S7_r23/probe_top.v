// S7 r2^3 timing probe: one r22-pipeline stage with the 45-degree (W8)
// rotation inserted, OOC.  D=128 (the pairs[0] shape of N=512; the S5
// probe showed the stage paths are size-independent).
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
        .Q8(92682),                       // round(sqrt2/2 * 2^17)
        .TWIDDLE_FILE("fft_twiddles_r22.mem")
    ) u_stage (
        .clk(clk), .ce(1'b1), .rst(rst),
        .in_re(in_re), .in_im(in_im),
        .out_re(out_re), .out_im(out_im)
    );

endmodule

`default_nettype wire
