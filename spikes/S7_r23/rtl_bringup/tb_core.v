// Full-core TB: fft_sdf_r23, parameterized by N.
`default_nettype none
`timescale 1ns/1ps
module tb_core #(parameter INV = 0, NUM_POINTS = 8192, NBLK = 2,
                  PACK = 32'h01555555);
    localparam integer N = NUM_POINTS;
    localparam integer T = N * NBLK;
    // the pipeline fill = ~7/8*N (the triple chain) + the pair lats;
    // plus slack for the golden-tail flush
    localparam integer TCLOCKS = T + (N * 7) / 8 + 700;
    reg clk = 0, rst = 1, ce = 1'b1;
    reg tv = 1'b1;
    reg [15:0] in_re = 0, in_im = 0;
    wire signed [15:0] out_re, out_im;
    wire mvalid, muser, mlast;
    reg su, sl;
    integer c, fd, f2;
    reg [31:0] stim [0:TCLOCKS-1];

    fft_sdf_r23 #(
        .NUM_POINTS(N), .SAMPLE_WIDTH(16),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .SCALING_PACK(PACK), .INVERSE(INV),
        .INTERN_WIDTH(16),
        .TWIDDLE_FILE_T0("fft_tw_r23_t0.mem"),
        .TWIDDLE_FILE_T1("fft_tw_r23_t1.mem"),
        .TWIDDLE_FILE_T2("fft_tw_r23_t2.mem"),
        .TWIDDLE_FILE_L("fft_tw_r22_l.mem")
    ) u_core (
        .clk(clk), .ce(ce), .rst(rst),
        .s_axis_tvalid(tv),
        .s_axis_tdata_re(in_re), .s_axis_tdata_im(in_im),
        .s_axis_tuser(su), .s_axis_tlast(sl),
        // debug tap: nothing (hierarchical refs below)
        .m_axis_tvalid(mvalid), .m_axis_tuser(muser), .m_axis_tlast(mlast),
        .m_axis_tdata_re(out_re), .m_axis_tdata_im(out_im)
    );

    // reference: the first triple standalone (G = N>>3)
    wire signed [15:0] sa_re, sa_im;
    fft_stage_r23 #(
        .DEPTH(N >> 3), .WIDTH(16), .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(N), .INVERSE(INV),
        .Q8(92682), .K_PRELOAD(16'h0),
        .TWIDDLE_FILE("fft_tw_r23_t0.mem")
    ) u_sa ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(in_re), .in_im(in_im), .out_re(sa_re), .out_im(sa_im) );


    integer nv, vn, ln;
    integer vu[0:7], vl[0:7];
    always #5 clk = ~clk;

    initial begin
        $readmemh("stim_core.mem", stim);
        fd = $fopen("out.hex", "w");
        nv = 0; vn = 0; ln = 0;
        f2 = $fopen("taps.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0;
        for (c = 0; c < TCLOCKS; c = c + 1) begin
            {in_im, in_re} = stim[c];
            su = (c % N == 0) && (c < T);
            sl = (c % N == N-1) && (c < T);
            @(posedge clk); #1;
            $fwrite(fd, "%04h %04h\n", out_re, out_im);
            $fwrite(f2, "%04h %04h %04h %04h %04h %04h %04h %04h %04h %04h %04h %04h\n",
                u_core.t1_re, u_core.t1_im,
                u_core.dbg_p0, u_core.dbg_p0i,
                u_core.dbg_p1, u_core.dbg_p1i,
                u_core.dbg_p2, u_core.dbg_p2i,
                u_core.dbg_p3, u_core.dbg_p3i,
                u_core.l0_re, u_core.l0_im);
            if (mvalid) begin
                nv = nv + 1;
                if (muser) begin
                    $display("TUSER at valid#%0d (c=%0d)", nv, c);
                    vu[vn] = nv; vn = vn + 1;
                end
                if (mlast) begin
                    $display("TLAST at valid#%0d (c=%0d)", nv, c);
                    vl[ln] = nv; ln = ln + 1;
                end
            end
            if (in_re !== u_core.u_t0.in_re || in_im !== u_core.u_t0.in_im)
                $display("INDIFF c=%0d tb=(%0d,%0d) core=(%0d,%0d)",
                    c, in_re, in_im, u_core.u_t0.in_re, u_core.u_t0.in_im);
            if (sa_re !== u_core.t1_re || sa_im !== u_core.t1_im)
                $display("T1DIFF c=%0d sa=(%0d,%0d) core=(%0d,%0d) k=%0d",
                    c, sa_re, sa_im, u_core.t1_re, u_core.t1_im,
                    u_core.u_t0.k);
            if (c >= 11266 && c <= 11272)
                $display("c=%0d k=%0d bm_r3=%d r1_r3=%d r3_r3=%d c1=(%d,%d) m_r=(%d,%d) tr=(%d,%d)",
                    c, u_core.u_t0.k, u_core.u_t0.bm_r3_re, u_core.u_t0.r1_r3_re,
                    u_core.u_t0.r3_r3_re, u_core.u_t0.c1_re, u_core.u_t0.c1_im,
                    u_core.u_t0.m_r_re, u_core.u_t0.m_r_im,
                    u_core.u_t0.tr_r, u_core.u_t0.ti_r);
            // pair-0 internals (D=64 N=2048 debug). k1 (not k): the
            // capture phase of the d_r/dl_r/s_r L0 regs held this period
            $display("PAIR c=%0d k1=%0d dr=%0d,%0d dl=%0d,%0d sr=%0d,%0d mr=%0d,%0d tr=%0d,%0d gr=%0d pfw=%0d,%0d gsd=%b gdl=%b out=%0d,%0d",
                c, u_core.lpairs[0].u_pair.k1,
                u_core.lpairs[0].u_pair.d_r_re, u_core.lpairs[0].u_pair.d_r_im,
                u_core.lpairs[0].u_pair.dl_r_re, u_core.lpairs[0].u_pair.dl_r_im,
                u_core.lpairs[0].u_pair.s_r_re, u_core.lpairs[0].u_pair.s_r_im,
                $signed(u_core.lpairs[0].u_pair.m_r_re), $signed(u_core.lpairs[0].u_pair.m_r_im),
                $signed(u_core.lpairs[0].u_pair.tr_r2), $signed(u_core.lpairs[0].u_pair.ti_r2),
                u_core.lpairs[0].u_pair.g_r,
                u_core.lpairs[0].u_pair.pwp, u_core.lpairs[0].u_pair.w_gate_pf,
                u_core.lpairs[0].u_pair.w_gate_sd, u_core.lpairs[0].u_pair.w_gate_dl,
                $signed(u_core.lpairs[0].u_pair.out_re), $signed(u_core.lpairs[0].u_pair.out_im));
            @(negedge clk);
        end
        $fclose(fd); $fclose(f2);
        $finish;
    end
endmodule
`default_nettype wire
