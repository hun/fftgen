`default_nettype none
`timescale 1ns/1ps
module tb_watch;
    reg clk = 0, rst = 1, ce = 0;
    reg signed [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] out_re, out_im;
    integer c;
    reg [31:0] stim [0:5000];

    fft_stage_r23 #(
        .DEPTH(128), .WIDTH(16), .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(1024), .INVERSE(0),
        .Q8(92682), .K_PRELOAD(16'h0),
        .TWIDDLE_FILE("fft_twiddles_r23.mem")
    ) u_stage ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(in_re), .in_im(in_im), .out_re(out_re), .out_im(out_im) );

    always #5 clk = ~clk;

    // watch the y4 window of block 0: k in [894, 935]
    always @(posedge clk) begin
        if (ce && c >= 1410 && c <= 1416)
            $display("k=%0d bm_r3=%d bp2_r3=%d r1_r3=%d r3_r3=%d c1=(%d,%d) c5=(%d,%d) c1r=(%d,%d)",
                u_stage.k, u_stage.bm_r3_re, u_stage.bp2_r3_re,
                u_stage.r1_r3_re, u_stage.r3_r3_re,
                u_stage.c1_re, u_stage.c1_im, u_stage.c5_re, u_stage.c5_im,
                u_stage.c1r_re, u_stage.c1r_im);
        if (ce && c >= 1410 && c <= 1416)
            $display("   k=%0d prod=(%d,%d)(%d,%d) p=(%d,%d) sh=(%d,%d) wpf4=%b a4=%d",
                u_stage.k, u_stage.prod_re_re, u_stage.prod_re_ti,
                u_stage.prod_im_re, u_stage.prod_im_ti,
                u_stage.p_re, u_stage.p_im,
                u_stage.shift_p_re, u_stage.shift_p_im,
                u_stage.w_pf4, u_stage.pw_addr4);
    end

    initial begin
        $readmemh("stim.mem", stim);
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0; ce = 1;
        for (c = 0; c < 2958; c = c + 1) begin
            {in_im, in_re} = stim[c];
            @(posedge clk); #1;
            @(negedge clk);
        end
        $finish;
    end
endmodule
`default_nettype wire
