`timescale 1ns/1ps
module tb;
    localparam integer W = 16;
    reg clk = 0, rst = 1;
    reg tvalid = 1;
    reg [2*W-1:0] stim [0:1024];
    integer c;
    wire mvalid;
    wire signed [W-1:0] ore, oim;
    wire signed [W-1:0] t0_re, t0_im, t1_re, t1_im;
    fft_sdf_r23_dit #(
        .NUM_POINTS(512), .SAMPLE_WIDTH(16), .INTERN_WIDTH(16),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .SCALING_PACK(64'h3f), .INVERSE(1),
        .TWIDDLE_FILE("tw_chain_512.mem"), .Q8(92682)
    ) dut (.clk(clk), .ce(1'b1), .rst(rst),
           .s_axis_tvalid(tvalid),
           .s_axis_tdata_re(stim[c][2*W-1:W]), .s_axis_tdata_im(stim[c][W-1:0]),
           .s_axis_tuser(1'b0), .s_axis_tlast(1'b0),
           .m_axis_tvalid(mvalid), .m_axis_tdata_re(ore), .m_axis_tdata_im(oim),
           .m_axis_tuser(), .m_axis_tlast());
    assign t0_re = dut.st_out_re[0]; assign t0_im = dut.st_out_im[0];
    assign t1_re = dut.st_out_re[1]; assign t1_im = dut.st_out_im[1];
    always #5 clk = ~clk;
    initial begin
        $readmemh("stim_chain_512.txt", stim);
        repeat (2) @(negedge clk);
        rst = 0;
        for (c = 0; c < 30; c = c + 1) begin
            @(posedge clk); #1;
            $display("c=%0d t0=(%h,%h) t1=(%h,%h) out=(%h,%h) k0=%0d k1=%0d k2=%0d",
                     c, t0_re, t0_im, t1_re, t1_im, ore, oim,
                     dut.trips[0].u_trip.k, dut.trips[1].u_trip.k,
                     dut.trips[2].u_trip.k);
            @(negedge clk);
        end
        $finish;
    end
endmodule
