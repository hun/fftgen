// Full-core TB: fft_sdf_r23, N=8192 shape.
`default_nettype none
`timescale 1ns/1ps
module tb_core #(parameter INV = 0, L0T = 0, L1T = 0);
    localparam integer TCLOCKS = 25000;
    reg clk = 0, rst = 1, ce = 1'b1;
    reg tv = 1'b1;
    reg [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] out_re, out_im;
    wire mvalid;
    integer c, fd, f2;
    reg [31:0] stim [0:TCLOCKS-1];

    fft_sdf_r23 #(
        .NUM_POINTS(8192), .SAMPLE_WIDTH(16),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .SCALING_PACK(32'h01555555), .INVERSE(INV),
        .KP_L0_TRIM(L0T), .KP_L1_TRIM(L1T),
        .INTERN_WIDTH(16),
        .TWIDDLE_FILE_T0("fft_tw_r23_t0.mem"),
        .TWIDDLE_FILE_T1("fft_tw_r23_t1.mem"),
        .TWIDDLE_FILE_T2("fft_tw_r23_t2.mem"),
        .TWIDDLE_FILE_L0("fft_tw_r22_l0.mem"),
        .TWIDDLE_FILE_L1("fft_tw_r22_l1.mem")
    ) u_core (
        .clk(clk), .ce(ce), .rst(rst),
        .s_axis_tvalid(tv),
        .s_axis_tdata_re(in_re), .s_axis_tdata_im(in_im),
        .s_axis_tuser(1'b0), .s_axis_tlast(1'b0),
        // debug tap: nothing (hierarchical refs below)
        .m_axis_tvalid(mvalid),
        .m_axis_tdata_re(out_re), .m_axis_tdata_im(out_im),
        .m_axis_tuser(), .m_axis_tlast()
    );

    always #5 clk = ~clk;

    initial begin
        $readmemh("stim_core.mem", stim);
        fd = $fopen("out.hex", "w");
        f2 = $fopen("t2.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0;
        for (c = 0; c < TCLOCKS; c = c + 1) begin
            {in_im, in_re} = stim[c];
            @(posedge clk); #1;
            $fwrite(fd, "%04h %04h\n", out_re, out_im);
            $fwrite(f2, "%04h %04h\n", u_core.t1_re, u_core.t1_im);
            if (c >= 11266 && c <= 11272)
                $display("c=%0d k=%0d bm_r3=%d r1_r3=%d r3_r3=%d c1=(%d,%d) m_r=(%d,%d) tr=(%d,%d)",
                    c, u_core.u_t0.k, u_core.u_t0.bm_r3_re, u_core.u_t0.r1_r3_re,
                    u_core.u_t0.r3_r3_re, u_core.u_t0.c1_re, u_core.u_t0.c1_im,
                    u_core.u_t0.m_r_re, u_core.u_t0.m_r_im,
                    u_core.u_t0.tr_r, u_core.u_t0.ti_r);
            @(negedge clk);
        end
        $fclose(fd); $fclose(f2);
        $finish;
    end
endmodule
`default_nettype wire
