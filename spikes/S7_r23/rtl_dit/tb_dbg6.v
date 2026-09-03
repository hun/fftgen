`timescale 1ns/1ps
module tb;
    localparam integer W = 16;
    reg clk = 0, rst = 1;
    reg tvalid = 1;
    reg [2*W-1:0] stim [0:8191];
    integer c;
    integer fd;
    wire signed [W-1:0] t0_re, t0_im, t1_re, t1_im, t2_re, t2_im, t3_re, t3_im;
    fft_sdf_r23_dit #(
        .NUM_POINTS(4096), .SAMPLE_WIDTH(16), .INTERN_WIDTH(16),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .SCALING_PACK(64'h0155555555), .INVERSE(1),
        .TWIDDLE_FILE("tw_chain_4096.mem"), .Q8(92682)
    ) dut (.clk(clk), .ce(1'b1), .rst(rst),
           .s_axis_tvalid(tvalid),
           .s_axis_tdata_re(stim[c][2*W-1:W]), .s_axis_tdata_im(stim[c][W-1:0]),
           .s_axis_tuser(1'b0), .s_axis_tlast(1'b0),
           .m_axis_tvalid(), .m_axis_tdata_re(), .m_axis_tdata_im(),
           .m_axis_tuser(), .m_axis_tlast());
    assign t0_re = dut.st_out_re[0]; assign t0_im = dut.st_out_im[0];
    assign t1_re = dut.st_out_re[1]; assign t1_im = dut.st_out_im[1];
    assign t2_re = dut.st_out_re[2]; assign t2_im = dut.st_out_im[2];
    assign t3_re = dut.st_out_re[3]; assign t3_im = dut.st_out_im[3];
    always #5 clk = ~clk;
    initial begin
        $readmemh("stim_chain_4096.txt", stim);
        fd = $fopen("dbg6_out.txt", "w");
        repeat (2) @(negedge clk);
        rst = 0;
        for (c = 0; c < 4200; c = c + 1) begin
            @(posedge clk); #1;
            $fwrite(fd, "%0d %h %h %h %h %h %h %h %h\n", c, t0_re, t0_im, t1_re, t1_im, t2_re, t2_im, t3_re, t3_im);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
