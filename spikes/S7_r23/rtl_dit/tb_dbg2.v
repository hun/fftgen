`timescale 1ns/1ps
module tb;
    localparam integer W = 16;
    localparam integer T = 3072;
    reg clk = 0, ce = 1, rst = 1;
    reg signed [W-1:0] in_re = 0, in_im = 0;
    wire signed [W-1:0] out_re, out_im;
    reg [2*W-1:0] stim [0:T-1];
    integer c;
    fft_stage_r23_dit #(
        .DEPTH(128), .WIDTH(16),
        .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(1024), .INVERSE(0), .Q8(92682),
        .K_PRELOAD(0), .TWIDDLE_FILE("tw_dit_1024_0.mem")
    ) dut (.clk(clk), .ce(ce), .rst(rst),
           .in_re(in_re), .in_im(in_im),
           .out_re(out_re), .out_im(out_im));
    always #5 clk = ~clk;
    initial begin
        $readmemh("stim_1024_0.txt", stim);
        repeat (2) @(negedge clk);
        rst = 0;
        for (c = 0; c < T; c = c + 1) begin
            in_re = $signed(stim[c][2*W-1:W]);
            in_im = $signed(stim[c][W-1:0]);
            @(posedge clk);
            #1;
            if (c == 896 || c == 897 || c == 1280) begin
                $display("c=%0d k=%0d w3win=%0d phase7=%b gq3=%0d q3[128]=(%0d,%0d) w3_q=%0d w4_q=%0d out=(%0d,%0d)",
                         c, dut.k, dut.w3, dut.phase7, dut.gq3,
                         $signed(dut.q3_re[128]), $signed(dut.q3_im[128]),
                         $signed(dut.w3_q), $signed(dut.w4_q),
                         $signed(out_re), $signed(out_im));
            end
            @(negedge clk);
        end
        $finish;
    end
endmodule
