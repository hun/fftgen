`timescale 1ns/1ps
module tb;
    localparam integer W = 16;
    localparam integer N = 32768;
    localparam integer LAT = 32772;
    localparam integer TOTAL = 2*N;
    reg clk = 0, rst = 1;
    reg tvalid = 1, tuser = 0, tlast = 0;
    reg [2*W-1:0] stim [0:TOTAL-1];
    integer c;
    integer fd;
    wire mvalid;
    wire signed [W-1:0] ore, oim;
    wire muser, mlast;
    fft_sdf_r23_dit #(
        .NUM_POINTS(32768), .SAMPLE_WIDTH(16), .INTERN_WIDTH(16),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .SCALING_PACK(64'h15555555),
        .INVERSE(1), .TWIDDLE_FILE("/home/hannes/Projects/fftgen/spikes/S7_r23/rtl_dit/tw_chain_32768.mem"), .Q8(92682),
        .R2B_I(16'hffff), .R2B_C(1'h0),
        .R2B_WPTR(16'hffff), .R2B_PWP(16'hffff),
        .R2B_RADDR(16'hffff), .R2B_PIPE(9'h0)
    ) dut (.clk(clk), .ce(1'b1), .rst(rst),
           .s_axis_tvalid(tvalid),
           .s_axis_tdata_re(stim[c][2*W-1:W]), .s_axis_tdata_im(stim[c][W-1:0]),
           .s_axis_tuser(tuser), .s_axis_tlast(tlast),
           .m_axis_tvalid(mvalid), .m_axis_tdata_re(ore), .m_axis_tdata_im(oim),
           .m_axis_tuser(muser), .m_axis_tlast(mlast));
    always #5 clk = ~clk;
    initial begin
        $readmemh("/home/hannes/Projects/fftgen/spikes/S7_r23/rtl_dit/stim_chain_32768.txt", stim);
        fd = $fopen("/home/hannes/Projects/fftgen/spikes/S7_r23/rtl_dit/rtl_chain_32768.txt", "w");
        repeat (2) @(negedge clk);
        rst = 0;
        for (c = 0; c < TOTAL + LAT + 4; c = c + 1) begin
            @(posedge clk);
            #1;
            if (c >= LAT - 1)
                $fwrite(fd, "%h %h\n", ore, oim);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
