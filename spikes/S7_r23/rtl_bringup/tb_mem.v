`default_nettype none
`timescale 1ns/1ps
module tb_mem;
    reg clk = 0, rst = 1, ce = 0;
    reg signed [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] out_re, out_im;
    integer c, fd;
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

    initial begin
        $readmemh("stim.mem", stim);
        fd = $fopen("mem.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0; ce = 1;
        for (c = 0; c < 4400; c = c + 1) begin
            {in_im, in_re} = stim[c];
            @(posedge clk); #1;
            $fwrite(fd, "%0d %d %d %d %d %d %d\n", u_stage.k,
                u_stage.rr1_re[u_stage.g_addr], u_stage.rr3_re[u_stage.g_addr],
                u_stage.rbbm_re[u_stage.g_addr], u_stage.rbbp_re[u_stage.g_addr],
                u_stage.ringB_q_re[u_stage.g_addr],
                u_stage.ringB_q1_re[u_stage.g_addr]);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
`default_nettype wire
