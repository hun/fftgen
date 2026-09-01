`default_nettype none
`timescale 1ns/1ps
module tb_ltail #(parameter KP0 = 10, KP1 = 1);
    localparam integer TCLOCKS = 20000;
    reg clk = 0, rst = 1, ce = 1;
    reg signed [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] m_re, m_im, out_re, out_im;
    integer c, fd;
    reg [31:0] stim [0:TCLOCKS-1];

    fft_stage_r22 #(
        .DEPTH(4), .WIDTH(16), .SIGMA0(1), .SIGMA1(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(8192), .INVERSE(0),
        .K_PRELOAD(16'd0 + KP0), .TWIDDLE_FILE("fft_tw_r22_l0.mem")
    ) u_l0 ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(in_re), .in_im(in_im), .out_re(m_re), .out_im(m_im) );

    fft_stage_r22 #(
        .DEPTH(1), .WIDTH(16), .SIGMA0(1), .SIGMA1(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(8192), .INVERSE(0),
        .K_PRELOAD(16'd0 + KP1), .TWIDDLE_FILE("fft_tw_r22_l1.mem")
    ) u_l1 ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(m_re), .in_im(m_im), .out_re(out_re), .out_im(out_im) );

    always #5 clk = ~clk;
    initial begin
        $readmemh("stim_ltail.mem", stim);
        fd = $fopen("lt.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0;
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
