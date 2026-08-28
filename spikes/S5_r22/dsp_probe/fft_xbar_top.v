// P7 crossbar timing probe: fft_cross alone, OOC.
// The SSR timing limiter after P7 step 7 lives entirely in here
// (u_cross/g_pre[*].pp*_reg), so a standalone synthesis reproduces it
// in ~30 s per variant.
`default_nettype none

module fft_xbar_top #(
    parameter integer NPTS = 64,
    parameter integer R    = 2
)(
    input  wire clk,
    input  wire rst,
    input  wire ce,
    input  wire in_valid,
    input  wire signed [R*16-1:0] din_re,
    input  wire signed [R*16-1:0] din_im,
    output wire                    out_valid,
    output wire signed [R*16-1:0]  dout_re,
    output wire signed [R*16-1:0]  dout_im
);

    fft_cross #(
        .NUM_POINTS(NPTS), .SSR(R),
        .OUT_WIDTH(16), .OUT_DECIMAL(0),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .WN_FILE("fft_wn.mem"), .INVERSE(0)
    ) u_cross (
        .clk(clk), .ce(ce), .rst(rst), .in_valid(in_valid),
        .din_re(din_re), .din_im(din_im),
        .out_valid(out_valid), .dout_re(dout_re), .dout_im(dout_im)
    );

endmodule

`default_nettype wire
