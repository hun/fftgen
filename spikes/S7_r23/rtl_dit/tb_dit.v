`timescale 1ns/1ps
module tb;
    localparam integer W = 16;
    localparam integer T = 384;
    reg clk = 0, ce = 1, rst = 1;
    reg signed [W-1:0] in_re = 0, in_im = 0;
    wire signed [W-1:0] out_re, out_im;
    reg [2*W-1:0] stim [0:T-1];
    integer c;
    integer fd;
    fft_stage_r23_dit #(
        .DEPTH(16), .WIDTH(16),
        .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(65536), .INVERSE(0), .Q8(92682),
        .K_PRELOAD(0), .TWIDDLE_FILE("/home/hannes/Projects/fftgen/spikes/S7_r23/rtl_dit/tw_dit_65536_3.mem")
    ) dut (.clk(clk), .ce(ce), .rst(rst),
           .in_re(in_re), .in_im(in_im),
           .out_re(out_re), .out_im(out_im));
    always #5 clk = ~clk;
    initial begin
        $readmemh("/home/hannes/Projects/fftgen/spikes/S7_r23/rtl_dit/stim_65536_3.txt", stim);
        fd = $fopen("/home/hannes/Projects/fftgen/spikes/S7_r23/rtl_dit/rtl_out_65536_3.txt", "w");
        repeat (2) @(negedge clk);
        rst = 0;
        for (c = 0; c < T; c = c + 1) begin
            in_re = $signed(stim[c][2*W-1:W]);
            in_im = $signed(stim[c][W-1:0]);
            @(posedge clk);
            #1;
            $fwrite(fd, "%h %h\n", out_re, out_im);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
