// fftgen -- one radix-2^2 DIF stage (P7), mirroring golden._R22DIFStage
// register-for-register.
//
// The stage merges the DIF stage pair (2m, 2m+1) into one 4-sample
// group with ONE shared complex multiplier (3 products per group
// instead of 4). Group depth D = N/4^{m+1}, twiddle stride 4^m
// (pre-sliced ROM: [T[g*4^m]], [T[2g*4^m]], [T[3g*4^m]]).
//
// Schedule per 4D-clock block (k = local position mod 4D):
//   k in [0, 2D):   a0/a1 raw store into ram (lag-2D ring); staggered
//                   products y1 (k<D) / y3 (k>=D) -> pfifo
//   k in [2D, 3D):  a2 arrives -> (a0,a2): s0/d0 into sram/dram
//   k in [3D, 4D):  a3 arrives -> (a1,a3): s1/d1; s0,s1 meet -> y0 out;
//                   y2 product -> pfifo; d1 into dline
//
// The d0/d1 operands for the staggered products are re-read from
// dram/dline at the current sp (read-old/write-new: the next write to
// that address is the next block's, so the value persists). Each
// product is staged D clocks in the pfifo (lag D). The registered
// output emits y0 during [3D, 4D) and the pfifo read otherwise, so
// position p's value emerges at clock p + 3D + 1 (latency 3D+1).

`default_nettype none

module fft_stage_r22 #(
    parameter integer DEPTH          = 4,    // D = N/4^{m+1}
    parameter integer WIDTH          = 16,   // internal sample width
    parameter integer SIGMA0         = 1,    // sub-stage 2m shift
    parameter integer SIGMA1         = 1,    // sub-stage 2m+1 shift
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = 17,
    parameter integer ROM_BASE       = 0,    // slice base (3D per pair)
    parameter integer NPTS           = 16,   // N (full R2² ROM size)
    parameter integer INVERSE        = 0,    // W^{N/4} = -j fwd, +j inv
    parameter [15:0] K_PRELOAD       = 16'h0, // phase offset (-upstream mod 4D)
    parameter TWIDDLE_FILE           = "fft_twiddles_r22.mem"
)(
    input  wire             clk,
    input  wire             ce,
    input  wire             rst,
    input  wire signed [WIDTH-1:0] in_re,
    input  wire signed [WIDTH-1:0] in_im,
    output reg  signed [WIDTH-1:0] out_re,
    output reg  signed [WIDTH-1:0] out_im
);

    localparam integer AW = (2*DEPTH > 1) ? $clog2(2*DEPTH) : 1;
    localparam integer DW = (DEPTH > 1) ? $clog2(DEPTH) : 1;
    localparam integer KW = (4*DEPTH > 1) ? $clog2(4*DEPTH) : 1;
    localparam integer BW = WIDTH + 1;              // butterfly width
    localparam integer CB = WIDTH + 2;              // combined-diff width
    localparam integer MWB = CB + TWIDDLE_WIDTH + 1;// product width
    localparam integer PW = WIDTH + TWIDDLE_WIDTH + 4; // shift staging width
    localparam integer TD_PLUS_S1 = TWIDDLE_DECIMAL + SIGMA1;
    localparam integer TD_PLUS_BOTH = TWIDDLE_DECIMAL + SIGMA0 + SIGMA1;

    // ------------------------------------------------------------------
    // memories (distributed, async read; single rings read-old/write-new
    // at the same address; pfifo is SDP: write pwp, read pr = pwp - D)
    // ------------------------------------------------------------------
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] ram_re   [0:2*DEPTH-1];
    reg signed [WIDTH-1:0] ram_im   [0:2*DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] sram_re  [0:DEPTH-1];
    reg signed [WIDTH-1:0] sram_im  [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] dram_re  [0:DEPTH-1];
    reg signed [WIDTH-1:0] dram_im  [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] dline_re [0:DEPTH-1];
    reg signed [WIDTH-1:0] dline_im [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] pfifo_re [0:2*DEPTH-1];
    reg signed [WIDTH-1:0] pfifo_im [0:2*DEPTH-1];

    reg [AW-1:0] rp;
    reg [AW-1:0] pwp;
    reg [AW-1:0] pr_r;
    reg [DW-1:0] sp;

    // phase counter (free-running, aligned to the input stream by
    // K_PRELOAD = (-upstream_latency) mod 4D)
    reg [KW-1:0] k;
    // g = k mod DEPTH (the low clog2(D) bits -- but for D=1 that is 0
    // bits, i.e. g is always 0; a 1-bit slice would give k mod 2)
    localparam integer GBITS = (DEPTH > 1) ? $clog2(DEPTH) : 0;
    wire [DW-1:0] g = (GBITS > 0) ? k[GBITS-1:0] : {DW{1'b0}};
    wire [KW-1:0] kk = k;
    // phase thresholds at KW width: a narrower declaration would truncate
    // (D=1: 2*DEPTH in 1 bit becomes 0 and phase_a2 is always true)
    localparam [KW-1:0] ONE_D   = DEPTH;
    localparam [KW-1:0] TWO_D   = 2 * DEPTH;
    localparam [KW-1:0] THREE_D = 3 * DEPTH;
    wire phase_y1  = (k <  ONE_D);
    wire phase_y3  = (k >= ONE_D && k < TWO_D);
    wire phase_a2  = (k >= TWO_D && k < THREE_D);
    wire phase_cmb = (k >= THREE_D);

    // twiddle ROM: full R2² table (N entries), this pair's slice at
    // [ROM_BASE, ROM_BASE + 3D); slice 0 = T[g*4^m] (y1),
    // slice 1 = T[2g*4^m] (y2), slice 2 = T[3g*4^m] (y3)
    localparam integer ROMW = (NPTS > 1) ? $clog2(NPTS) : 1;
    (* ram_style = "distributed" *)
    reg signed [TWIDDLE_WIDTH*2-1:0] tw_rom [0:NPTS-1];
    initial $readmemh(TWIDDLE_FILE, tw_rom);
    localparam [ROMW-1:0] SLICE0 = 0;
    localparam [ROMW-1:0] SLICE1 = DEPTH;
    localparam [ROMW-1:0] SLICE2 = 2 * DEPTH;
    wire [ROMW-1:0] rom_which = phase_y1 ? SLICE0
                              : (phase_y3 ? SLICE2 : SLICE1);
    wire [ROMW-1:0] rom_addr = ROM_BASE[ROMW-1:0] + rom_which
                               + {{(ROMW-DW){1'b0}}, g};
    wire signed [TWIDDLE_WIDTH-1:0] tr = tw_rom[rom_addr]
        [TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
    wire signed [TWIDDLE_WIDTH-1:0] ti = tw_rom[rom_addr]
        [TWIDDLE_WIDTH-1:0];

    // round-half-up arithmetic right shift (BW domain, sums)
    function signed [BW-1:0] round_shift_bw;
        input signed [BW-1:0] v;
        input integer sh;
        begin
            if (sh <= 0)
                round_shift_bw = v;
            else
                round_shift_bw = (v + ($signed({{(BW-1){1'b0}}, 1'b1})
                                       <<< (sh-1))) >>> sh;
        end
    endfunction
    // round-half-up arithmetic right shift (PW domain, products)
    function signed [PW-1:0] round_shift_pw;
        input signed [PW-1:0] v;
        input integer sh;
        begin
            if (sh <= 0)
                round_shift_pw = v;
            else
                round_shift_pw = (v + ($signed({{(PW-1){1'b0}}, 1'b1})
                                       <<< (sh-1))) >>> sh;
        end
    endfunction

    wire signed [BW-1:0] x_re = {{(BW-WIDTH){in_re[WIDTH-1]}}, in_re};
    wire signed [BW-1:0] x_im = {{(BW-WIDTH){in_im[WIDTH-1]}}, in_im};

    // memory reads (async; read-old at the current pointer)
    wire signed [BW-1:0] a0_re = {{(BW-WIDTH){ram_re[rp][WIDTH-1]}}, ram_re[rp]};
    wire signed [BW-1:0] a0_im = {{(BW-WIDTH){ram_im[rp][WIDTH-1]}}, ram_im[rp]};
    wire signed [BW-1:0] s0_re = {{(BW-WIDTH){sram_re[sp][WIDTH-1]}}, sram_re[sp]};
    wire signed [BW-1:0] s0_im = {{(BW-WIDTH){sram_im[sp][WIDTH-1]}}, sram_im[sp]};
    wire signed [BW-1:0] d0_re = {{(BW-WIDTH){dram_re[sp][WIDTH-1]}}, dram_re[sp]};
    wire signed [BW-1:0] d0_im = {{(BW-WIDTH){dram_im[sp][WIDTH-1]}}, dram_im[sp]};
    wire signed [BW-1:0] d1_re = {{(BW-WIDTH){dline_re[sp][WIDTH-1]}}, dline_re[sp]};
    wire signed [BW-1:0] d1_im = {{(BW-WIDTH){dline_im[sp][WIDTH-1]}}, dline_im[sp]};

    // sub-stage 2m butterfly: s = round(a + x, SIGMA0), d = a - x
    // (a = ram[rp]: a0 at the a2 clock, a1 at the a3 clock)
    wire signed [BW-1:0] s_x_re = round_shift_bw(a0_re + x_re, SIGMA0);
    wire signed [BW-1:0] s_x_im = round_shift_bw(a0_im + x_im, SIGMA0);
    wire signed [BW-1:0] d_x_re = a0_re - x_re;
    wire signed [BW-1:0] d_x_im = a0_im - x_im;

    // y0 = round(s0 + s1, SIGMA1)   (combine clock)
    wire signed [BW-1:0] y0_re = round_shift_bw(s0_re + s_x_re, SIGMA1);
    wire signed [BW-1:0] y0_im = round_shift_bw(s0_im + s_x_im, SIGMA1);

    // combined diffs (CB): y1 = d0 -/+ j*d1, y3 = d0 +/- j*d1
    //   js = -1 fwd, +1 inv;  y1: (d0_re + js*d1_im, d0_im - js*d1_re)
    //                         y3: (d0_re - js*d1_im, d0_im + js*d1_re)
    wire signed [CB-1:0] se_d0_re = {{(CB-BW){d0_re[BW-1]}}, d0_re};
    wire signed [CB-1:0] se_d0_im = {{(CB-BW){d0_im[BW-1]}}, d0_im};
    wire signed [CB-1:0] se_d1_re = {{(CB-BW){d1_re[BW-1]}}, d1_re};
    wire signed [CB-1:0] se_d1_im = {{(CB-BW){d1_im[BW-1]}}, d1_im};
    wire signed [CB-1:0] js_d1_im = INVERSE ? -se_d1_im : se_d1_im;
    wire signed [CB-1:0] js_d1_re = INVERSE ? -se_d1_re : se_d1_re;
    wire signed [CB-1:0] c1_re = se_d0_re + js_d1_im;   // d0 + js*d1_im
    wire signed [CB-1:0] c1_im = se_d0_im - js_d1_re;   // d0 - js*d1_re
    wire signed [CB-1:0] c3_re = se_d0_re - js_d1_im;
    wire signed [CB-1:0] c3_im = se_d0_im + js_d1_re;

    // multiplier operand mux (CB wide):
    //   y2: (s0 - s1)  at phase_cmb;  y1: c1 at phase_y1;  y3: c3 at phase_y3
    wire signed [BW-1:0] sd_re = s0_re - s_x_re;
    wire signed [BW-1:0] sd_im = s0_im - s_x_im;
    wire signed [CB-1:0] sdiff_re = {{(CB-BW){sd_re[BW-1]}}, sd_re};
    wire signed [CB-1:0] sdiff_im = {{(CB-BW){sd_im[BW-1]}}, sd_im};
    wire signed [CB-1:0] m_re = phase_cmb ? sdiff_re : (phase_y1 ? c1_re : c3_re);
    wire signed [CB-1:0] m_im = phase_cmb ? sdiff_im : (phase_y1 ? c1_im : c3_im);

    // products (exact, MWB wide): widen both operands to MWB first
    wire signed [MWB-1:0] m_re_w = {{(MWB-CB){m_re[CB-1]}}, m_re};
    wire signed [MWB-1:0] m_im_w = {{(MWB-CB){m_im[CB-1]}}, m_im};
    wire signed [MWB-1:0] tr_w = {{(MWB-TWIDDLE_WIDTH){tr[TWIDDLE_WIDTH-1]}}, tr};
    wire signed [MWB-1:0] ti_w = {{(MWB-TWIDDLE_WIDTH){ti[TWIDDLE_WIDTH-1]}}, ti};
    wire signed [MWB-1:0] prod_rr = m_re_w * tr_w;    // re*tr
    wire signed [MWB-1:0] prod_ri = m_re_w * ti_w;    // re*ti
    wire signed [MWB-1:0] prod_ir = m_im_w * tr_w;    // im*tr
    wire signed [MWB-1:0] prod_ii = m_im_w * ti_w;    // im*ti

    // fused shifts
    wire signed [PW-1:0] y2_r = round_shift_pw(
        {{(PW-MWB){prod_rr[MWB-1]}}, prod_rr} -
        {{(PW-MWB){prod_ii[MWB-1]}}, prod_ii}, TD_PLUS_S1);
    wire signed [PW-1:0] y2_i = round_shift_pw(
        {{(PW-MWB){prod_ri[MWB-1]}}, prod_ri} +
        {{(PW-MWB){prod_ir[MWB-1]}}, prod_ir}, TD_PLUS_S1);
    wire signed [PW-1:0] y13_r = round_shift_pw(
        {{(PW-MWB){prod_rr[MWB-1]}}, prod_rr} -
        {{(PW-MWB){prod_ii[MWB-1]}}, prod_ii}, TD_PLUS_BOTH);
    wire signed [PW-1:0] y13_i = round_shift_pw(
        {{(PW-MWB){prod_ri[MWB-1]}}, prod_ri} +
        {{(PW-MWB){prod_ir[MWB-1]}}, prod_ir}, TD_PLUS_BOTH);

    // output mux (registered): y0 during [3D,4D), pfifo otherwise
    wire signed [WIDTH-1:0] mux_re = phase_cmb ? y0_re[WIDTH-1:0]
                                               : pfifo_re[pr_r];
    wire signed [WIDTH-1:0] mux_im = phase_cmb ? y0_im[WIDTH-1:0]
                                               : pfifo_im[pr_r];

    always @(posedge clk) begin
        if (rst) begin
            k      <= K_PRELOAD[KW-1:0];
            rp     <= {AW{1'b0}};
            pwp    <= {AW{1'b0}};
            pr_r   <= {AW{1'b0}} - DEPTH[AW-1:0];
            sp     <= {DW{1'b0}};
            out_re <= {WIDTH{1'b0}};
            out_im <= {WIDTH{1'b0}};
        end else if (ce) begin
            out_re <= mux_re;
            out_im <= mux_im;

            // a0/a1 raw store + staggered products y1/y3 -> pfifo
            if (phase_y1 || phase_y3) begin
                ram_re[rp] <= in_re;
                ram_im[rp] <= in_im;
                pfifo_re[pwp] <= y13_r[WIDTH-1:0];
                pfifo_im[pwp] <= y13_i[WIDTH-1:0];
            end
            // a2: s0/d0 into the lag-D lines
            if (phase_a2) begin
                sram_re[sp] <= s_x_re[WIDTH-1:0];
                sram_im[sp] <= s_x_im[WIDTH-1:0];
                dram_re[sp] <= d_x_re[WIDTH-1:0];
                dram_im[sp] <= d_x_im[WIDTH-1:0];
            end
            // a3: y2 -> pfifo, d1 -> dline
            if (phase_cmb) begin
                pfifo_re[pwp] <= y2_r[WIDTH-1:0];
                pfifo_im[pwp] <= y2_i[WIDTH-1:0];
                dline_re[sp] <= d_x_re[WIDTH-1:0];
                dline_im[sp] <= d_x_im[WIDTH-1:0];
            end

            k    <= k + {{(KW-1){1'b0}}, 1'b1};
            rp   <= rp + {{(AW-1){1'b0}}, 1'b1};
            pwp  <= pwp + {{(AW-1){1'b0}}, 1'b1};
            pr_r <= pwp + {{(AW-1){1'b0}}, 1'b1} - DEPTH[AW-1:0];
            // sp wraps at DEPTH-1 (a free-running counter is mod 2^DW,
            // which only equals D for D >= 2; D=1 would count 0,1,0,1)
            if (sp == DEPTH[DW-1:0] - 1'b1)
                sp <= {DW{1'b0}};
            else
                sp <= sp + {{(DW-1){1'b0}}, 1'b1};
        end
    end

endmodule

`default_nettype wire
