// S7 RTL probe: one radix-2^3 stage (the first triple shape of N=1024:
// G = 128), OOC on KU5P @ 2 ns.
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

    fft_stage_r23 #(
        .DEPTH(DEPTH),
        .WIDTH(16),
        .SIGMA0(1),
        .SIGMA1(1),
        .SIGMA2(1),
        .TWIDDLE_WIDTH(18),
        .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0),
        .NPTS(8 * DEPTH),
        .INVERSE(0),
        .Q8(92682),
        .K_PRELOAD(16'h0),
        .TWIDDLE_FILE("fft_twiddles_r23.mem")
    ) u_stage (
        .clk(clk), .ce(1'b1), .rst(rst),
        .in_re(in_re), .in_im(in_im),
        .out_re(out_re), .out_im(out_im)
    );

endmodule

`default_nettype wire
