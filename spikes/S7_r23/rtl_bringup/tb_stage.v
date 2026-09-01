// S7 bring-up TB: one r23 stage vs the golden _R23DIFStage stream.
// stim.mem: one 32-bit word per clock, {im[15:0], re[15:0]}.
// Dumps one line per clock: "<re_hex4> <im_hex4>".
`default_nettype none
`timescale 1ns/1ps
module tb_stage #(parameter INV = 0, DEPTH = 128);
    localparam integer TCLOCKS = 630;   // stim + golden latency tail
    reg clk = 0, rst = 1, ce = 0;
    reg signed [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] out_re, out_im;
    integer fd, c;
    reg [31:0] stim [0:TCLOCKS-1];
    reg [15:0] vre, vim;

    fft_stage_r23 #(
        .DEPTH(DEPTH), .WIDTH(16),
        .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(8*DEPTH), .INVERSE(INV),
        .Q8(92682), .K_PRELOAD(16'h0),
        .TWIDDLE_FILE("fft_twiddles_r23.mem")
    ) u_stage (
        .clk(clk), .ce(ce), .rst(rst),
        .in_re(in_re), .in_im(in_im),
        .out_re(out_re), .out_im(out_im)
    );

    always #5 clk = ~clk;

    initial begin
        $readmemh("stim.mem", stim);
        fd = $fopen("out.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0; ce = 1;
        for (c = 0; c < TCLOCKS; c = c + 1) begin
            {vim, vre} = stim[c];
            in_re <= $signed(vre);
            in_im <= $signed(vim);
            @(posedge clk); #1;
            $fwrite(fd, "%04h %04h\n", out_re, out_im);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
`default_nettype wire
