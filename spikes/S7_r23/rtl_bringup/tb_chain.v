// Two-stage r23 chain (N=1024 shape): G1=128 (m=0) -> G2=16 (m=1).
// Stage 2's phase must run (c - 906) mod 128 with 906 = 7*128+2+H (H=8):
// K_PRELOAD2 = -906 mod 128 = 118.
`default_nettype none
`timescale 1ns/1ps
module tb_chain #(parameter INV = 0, KP2 = 118);
    localparam integer TCLOCKS = 7972;
    reg clk = 0, rst = 1, ce = 0;
    reg signed [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] o1_re, o1_im, out_re, out_im;
    integer c, fd;
    reg [31:0] stim [0:TCLOCKS-1];

    fft_stage_r23 #(
        .DEPTH(128), .WIDTH(16), .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(1024), .INVERSE(INV),
        .Q8(92682), .K_PRELOAD(16'h0),
        .TWIDDLE_FILE("fft_tw_r23_g128.mem")
    ) u1 ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(in_re), .in_im(in_im), .out_re(o1_re), .out_im(o1_im) );

    fft_stage_r23 #(
        .DEPTH(16), .WIDTH(16), .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(128), .INVERSE(INV),
        .Q8(92682), .K_PRELOAD(16'd0 + KP2),
        .TWIDDLE_FILE("fft_tw_r23_g16.mem")
    ) u2 ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(o1_re), .in_im(o1_im), .out_re(out_re), .out_im(out_im) );

    always #5 clk = ~clk;

    initial begin
        $readmemh("stim.mem", stim);
        fd = $fopen("out.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0; ce = 1;
        for (c = 0; c < TCLOCKS; c = c + 1) begin
            {in_im, in_re} = stim[c];
            @(posedge clk); #1;
            $fwrite(fd, "%04h %04h\n", out_re, out_im);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
`default_nettype wire
