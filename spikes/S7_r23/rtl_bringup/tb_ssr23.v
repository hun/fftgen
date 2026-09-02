// SSR R=2 r23 TB: fft_ssr_r23, R words per clock, markers, output dump.
// stim word layout (LSB first): re0[15:0], re1[15:0], im0[15:0], im1[15:0],
// tuser(bit 64), tlast(bit 65)
`default_nettype none
`timescale 1ns/1ps
module tb_ssr23 #(parameter INV = 0, NUM_POINTS = 1024, PACK = 32'h015555);
    localparam integer N = NUM_POINTS;
    localparam integer R = 2;
    localparam integer TCLOCKS = N * 8 + 800;
    reg clk = 0, rst = 1, ce = 1'b1;
    reg tv = 1'b1;
    reg [65:0] stim [0:TCLOCKS-1];
    reg su, sl;
    wire [31:0] re_packed, im_packed;
    wire [31:0] ore_flat, oim_flat;
    wire mvalid, mu, ml;
    integer c, fd;

    assign re_packed = stim[c][31:0];
    assign im_packed = stim[c][63:32];
    fft_ssr_r23 #(
        .NUM_POINTS(N), .SSR(R), .SAMPLE_WIDTH(16), .SAMPLE_DECIMAL(0),
        .OUTPUT_WIDTH(16), .OUTPUT_DECIMAL(0),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .SCALING_PACK(PACK), .INTERN_WIDTH(16),
        .INVERSE(INV),
        .WN_FILE("fft_wn.mem"),
        .LANE_TW_T0("fft_tw_r23_t0.mem"),
        .LANE_TW_T1("fft_tw_r23_t1.mem"),
        .LANE_TW_T2("fft_tw_r23_t2.mem"),
        .LANE_TW_L("fft_tw_r22_l.mem")
    ) u_dut (
        .clk(clk), .ce(ce), .rst(rst),
        .s_axis_tvalid(tv),
        .s_axis_tdata_re(re_packed), .s_axis_tdata_im(im_packed),
        .s_axis_tuser(su), .s_axis_tlast(sl),
        .m_axis_tvalid(mvalid),
        .m_axis_tdata_re(ore_flat), .m_axis_tdata_im(oim_flat),
        .m_axis_tuser(mu), .m_axis_tlast(ml)
    );

    always #5 clk = ~clk;
    initial begin
        $readmemh("stim_ssr.mem", stim);
        fd = $fopen("out_ssr.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0;
        for (c = 0; c < TCLOCKS; c = c + 1) begin
            if (c < N * 4) begin
                su = stim[c][64]; sl = stim[c][65];
            end else begin
                su = 0; sl = 0;
            end
            #1; // markers settle with the word (before the edge)
            @(posedge clk); #1;
            if (mvalid)
                $fwrite(fd, "%04h %04h %04h %04h %0d %0d\n",
                    ore_flat[15:0], oim_flat[15:0],
                    ore_flat[31:16], oim_flat[31:16], mu, ml);
            if (u_dut.u_cross.in_valid)
                $display("LANE c=%0d cnt_p=%0d p_rom=%0d l0=%0d,%0d l1=%0d,%0d",
                    c, u_dut.u_cross.p, u_dut.u_cross.p_rom,
                    $signed(u_dut.xb_re[15:0]), $signed(u_dut.xb_im[15:0]),
                    $signed(u_dut.xb_re[31:16]), $signed(u_dut.xb_im[31:16]));
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
`default_nettype wire
