`timescale 1ns/1ps
module sim_smoke;
    reg clk = 0, rst = 1;
    reg signed [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] out_re, out_im;
    integer i, changes = 0;
    reg signed [15:0] prev = 0;
    fft_probe_top dut(.clk(clk), .rst(rst), .in_re(in_re), .in_im(in_im),
                      .out_re(out_re), .out_im(out_im));
    always #5 clk = ~clk;
    initial begin
        for (i = 0; i < 4; i = i + 1) begin @(posedge clk); end
        rst = 0;
        for (i = 0; i < 1200; i = i + 1) begin
            in_re <= $random; in_im <= $random;
            @(posedge clk);
            if (out_re !== prev) begin changes = changes + 1; prev = out_re; end
        end
        $display("output changes: %0d (expect many if datapath alive)", changes);
        $finish;
    end
endmodule
