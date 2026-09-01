`default_nettype none
`timescale 1ns/1ps
module tb_pair();
    localparam integer D = 64;
    reg clk = 0, rst = 1, ce = 1'b1;
    reg signed [15:0] in_re, in_im;
    wire signed [15:0] out_re, out_im;
    integer c, fd, i;
    reg [31:0] stim [0:2347];
    fft_stage_r22 #(
        .DEPTH(D), .WIDTH(16), .SIGMA0(1), .SIGMA1(1),
        .TWIDDLE_WIDTH(18), .TWIDDLE_DECIMAL(17),
        .ROM_BASE(0), .NPTS(192), .INVERSE(0),
        .K_PRELOAD(16'h0), .TWIDDLE_FILE("tw_pair0.mem")
    ) u_st ( .clk(clk), .ce(ce), .rst(rst),
        .in_re(in_re), .in_im(in_im), .out_re(out_re), .out_im(out_im) );
    always #5 clk = ~clk;
    initial begin
        $readmemh("stim.mem", stim);
        fd = $fopen("out.hex", "w");
        repeat (2) @(posedge clk);
        @(negedge clk); rst = 0;
        for (c = 0; c < 2340; c = c + 1) begin
            {in_im, in_re} = stim[c];
            @(posedge clk); #1;
            $fwrite(fd, "%04h %04h\n", out_re, out_im);
            $display("PTR c=%0d k=%0d sp=%0d rp=%0d pwp=%0d pr=%0d gsd=%b gdl=%b gpf=%b mr=%0d,%0d tr=%0d,%0d p=%0d,%0d sh=%0d,%0d",
                c, u_st.k, u_st.sp, u_st.rp, u_st.pwp, u_st.pr_r,
                u_st.w_gate_sd, u_st.w_gate_dl, u_st.w_gate_pf,
                $signed(u_st.m_r_re), $signed(u_st.m_r_im),
                $signed(u_st.tr_r2), $signed(u_st.ti_r2),
                $signed(u_st.p_re), $signed(u_st.p_im),
                $signed(u_st.shift_p_re), $signed(u_st.shift_p_im));
            if (c % 256 == 0 && c >= 1024 && c <= 1800) begin
                for (i = 0; i < D; i = i + 1)
                    $display("MEM c=%0d dram %0d %0d %0d", c, i,
                        $signed(u_st.dram_re[i]), $signed(u_st.dram_im[i]));
                for (i = 0; i < D; i = i + 1)
                    $display("MEM c=%0d dline %0d %0d %0d", c, i,
                        $signed(u_st.dline_re[i]), $signed(u_st.dline_im[i]));
            end
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
`default_nettype wire
