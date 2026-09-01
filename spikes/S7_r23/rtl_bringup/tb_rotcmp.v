// Two identical G=1024 r23 stages: one fed directly (standalone), one
// inside the 3-stage core wiring -- compare the rot pipelines.
`default_nettype none
`timescale 1ns/1ps
module tb_rotcmp;
    reg clk = 0, rst = 1, ce = 1;
    reg signed [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] s_re, s_im, c_re, c_im;
    integer c, fd, i;
    reg [31:0] stim [0:25000];

    fft_stage_r23 #(
        .DEPTH(1024), .WIDTH(16), .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(8192), .INVERSE(0),
        .Q8(92682), .K_PRELOAD(16'h0),
        .TWIDDLE_FILE("fft_tw_r23_t0.mem")
    ) u_sa ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(in_re), .in_im(in_im), .out_re(s_re), .out_im(s_im) );

    // a second copy whose output feeds a second stage (like the core)
    wire signed [15:0] d_re, d_im;
    fft_stage_r23 #(
        .DEPTH(1024), .WIDTH(16), .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(8192), .INVERSE(0),
        .Q8(92682), .K_PRELOAD(16'h0),
        .TWIDDLE_FILE("fft_tw_r23_t0.mem")
    ) u_ca ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(in_re), .in_im(in_im), .out_re(c_re), .out_im(c_im) );
    fft_stage_r23 #(
        .DEPTH(128), .WIDTH(16), .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(1024), .INVERSE(0),
        .Q8(92682), .K_PRELOAD(16'd1013),
        .TWIDDLE_FILE("fft_tw_r23_t1.mem")
    ) u_cb ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(c_re), .in_im(c_im), .out_re(d_re), .out_im(d_im) );

    always #5 clk = ~clk;

    initial begin
        $readmemh("stim_core.mem", stim);
        fd = $fopen("rotcmp.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0;
        for (c = 0; c < 12400; c = c + 1) begin
            {in_im, in_re} = stim[c];
            @(posedge clk); #1;
            if (c >= 7170 && c < 25000 && (s_re != c_re || s_im != c_im))
                $fwrite(fd, "OUTDIFF c=%0d sa=(%d,%d) ca=(%d,%d)\n",
                    c, s_re, s_im, c_re, c_im);
            if (c >= 11266 && c <= 11276)
                $fwrite(fd, "c=%0d sak=%0d cak=%0d sa: bm=%d r1=%d r3=%d c1=(%d,%d) | ca: bm=%d r1=%d r3=%d c1=(%d,%d)\n",
                    c, u_sa.k, u_ca.k,
                    u_sa.bm_r3_re, u_sa.r1_r3_re, u_sa.r3_r3_re,
                    u_sa.c1_re, u_sa.c1_im,
                    u_ca.bm_r3_re, u_ca.r1_r3_re, u_ca.r3_r3_re,
                    u_ca.c1_re, u_ca.c1_im);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
`default_nettype wire
