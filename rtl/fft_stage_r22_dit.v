// fftgen -- one radix-2^2 DIT stage (P7), mirroring golden._R22DITStage
// register-for-register.
//
// The DIT stage (multiply-then-combine) merges the pair (A, A+1) into a
// 4-sample group with THREE products on the inputs:
//
//   k in [0, H):    a0 -> v0 = a0 << td -> vline (depth 3H)
//   k in [H, 2H):   a1 -> t1 = cmul(a1, T[2j*4^m']) -> t1line (2H)
//   k in [2H, 3H):  a2 -> t2 = cmul(a2, T[j*4^m'])   -> t2line (H)
//   k in [3H, 4H):  a3 -> t3 = cmul(a3, T[3j*4^m']); F4 combine:
//                   y0 = round(v0 + t1 + t2 + t3, S)      pos j
//                   y2 = round(v0 + t1 - t2 - t3, S)      pos j+2H
//                   y1 = round(v0 - t1 + j*t2 - j*t3, S)  pos j+H
//                   y3 = round(v0 - t1 - j*t2 + j*t3, S)  pos j+3H
//
// ONE shared complex multiplier computes t1/t2/t3 at the arrivals (75%
// duty). The F4 combine uses the exact +/-j rotations. Position p's
// value emerges at clock p + 3H (latency 3H+1 with the output register):
// y0 at [3H,4H), y1 via the H-queue at [4H,5H), y2 at [5H,6H), y3 at
// [6H,7H) (the queues spill into the next block's [0,3H) window).
//
// ROM layout: per pair, 3H entries in the DIT order:
//   slice 0 = T[2j*4^m'] (t1), slice 1 = T[j*4^m'] (t2),
//   slice 2 = T[3j*4^m'] (t3).

`default_nettype none

module fft_stage_r22_dit #(
    parameter integer DEPTH          = 1,    // H = 2^A
    parameter integer WIDTH          = 16,
    parameter integer SIGMA0         = 1,    // stage A shift
    parameter integer SIGMA1         = 1,    // stage A+1 shift
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = 17,
    parameter integer ROM_BASE       = 0,
    parameter integer NPTS           = 16,
    parameter integer INVERSE        = 0,    // W^{N/4} = -j fwd, +j inv
    parameter [15:0] K_PRELOAD       = 16'h0,
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

    localparam integer AW3 = (3*DEPTH > 1) ? $clog2(3*DEPTH) : 1;
    localparam integer AW2 = (2*DEPTH > 1) ? $clog2(2*DEPTH) : 1;
    localparam integer AW1 = (DEPTH > 1) ? $clog2(DEPTH) : 1;
    localparam integer KW = (4*DEPTH > 1) ? $clog2(4*DEPTH) : 1;
    localparam integer GBITS = (DEPTH > 1) ? $clog2(DEPTH) : 0;
    localparam integer CB = WIDTH + TWIDDLE_WIDTH + 2;  // product width
    localparam integer CW = WIDTH + TWIDDLE_WIDTH + 6;  // combine width
    localparam integer PW = WIDTH + TWIDDLE_WIDTH + 4;
    localparam integer S_TOTAL = TWIDDLE_DECIMAL + SIGMA0 + SIGMA1;

    // ------------------------------------------------------------------
    // memories (distributed, async read; read-old/write-new rings)
    // ------------------------------------------------------------------
    (* ram_style = "distributed" *)
    reg signed [WIDTH+TWIDDLE_WIDTH-1:0] vline_re  [0:3*DEPTH-1];
    reg signed [WIDTH+TWIDDLE_WIDTH-1:0] vline_im  [0:3*DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [CB-1:0] t1line_re [0:2*DEPTH-1];
    reg signed [CB-1:0] t1line_im [0:2*DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [CB-1:0] t2line_re [0:DEPTH-1];
    reg signed [CB-1:0] t2line_im [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] q1_re [0:DEPTH-1];
    reg signed [WIDTH-1:0] q1_im [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] q2_re [0:2*DEPTH-1];
    reg signed [WIDTH-1:0] q2_im [0:2*DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] q3_re [0:3*DEPTH-1];
    reg signed [WIDTH-1:0] q3_im [0:3*DEPTH-1];

    reg [AW3-1:0] vp;
    reg [AW2-1:0] t1p;
    reg [AW1-1:0] t2p;
    reg [AW1-1:0] q1p;
    reg [AW2-1:0] q2p;
    reg [AW3-1:0] q3p;

    // phase counter (aligned to the input stream by K_PRELOAD)
    reg [KW-1:0] k;
    localparam [KW-1:0] ONE_H   = DEPTH;
    localparam [KW-1:0] TWO_H   = 2 * DEPTH;
    localparam [KW-1:0] THREE_H = 3 * DEPTH;
    wire [AW1-1:0] j = (GBITS > 0) ? k[GBITS-1:0] : {AW1{1'b0}};
    wire phase_a0 = (k <  ONE_H);
    wire phase_a1 = (k >= ONE_H && k < TWO_H);
    wire phase_a2 = (k >= TWO_H && k < THREE_H);
    wire phase_a3 = (k >= THREE_H);
    wire phase_q1 = (k <  ONE_H);
    wire phase_q2 = (k >= ONE_H && k < TWO_H);
    wire phase_q3 = (k >= TWO_H && k < THREE_H);

    // twiddle ROM: slice 0 = T[2j*4^m'] (t1), slice 1 = T[j*4^m'] (t2),
    // slice 2 = T[3j*4^m'] (t3)
    localparam integer ROMW = (NPTS > 1) ? $clog2(NPTS) : 1;
    (* ram_style = "distributed" *)
    reg signed [TWIDDLE_WIDTH*2-1:0] tw_rom [0:NPTS-1];
    initial $readmemh(TWIDDLE_FILE, tw_rom);
    localparam [ROMW-1:0] SL0 = 0;
    localparam [ROMW-1:0] SL1 = DEPTH;
    localparam [ROMW-1:0] SL2 = 2 * DEPTH;
    wire [ROMW-1:0] rom_which = phase_a1 ? SL0 : (phase_a2 ? SL1 : SL2);
    wire [ROMW-1:0] rom_addr = ROM_BASE[ROMW-1:0] + rom_which
                               + {{(ROMW-AW1){1'b0}}, j};
    wire signed [TWIDDLE_WIDTH-1:0] tr = tw_rom[rom_addr]
        [TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
    wire signed [TWIDDLE_WIDTH-1:0] ti = tw_rom[rom_addr]
        [TWIDDLE_WIDTH-1:0];

    // round-half-up arithmetic right shift
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

    // multiplier operand: the arriving sample (a1/a2/a3), product = x*w
    wire signed [WIDTH-1:0] m_re = in_re;
    wire signed [WIDTH-1:0] m_im = in_im;
    wire signed [CB-1:0] m_re_w = {{(CB-WIDTH){m_re[WIDTH-1]}}, m_re};
    wire signed [CB-1:0] m_im_w = {{(CB-WIDTH){m_im[WIDTH-1]}}, m_im};
    wire signed [CB-1:0] tr_w = {{(CB-TWIDDLE_WIDTH){tr[TWIDDLE_WIDTH-1]}}, tr};
    wire signed [CB-1:0] ti_w = {{(CB-TWIDDLE_WIDTH){ti[TWIDDLE_WIDTH-1]}}, ti};
    wire signed [CB-1:0] prod_r = m_re_w * tr_w - m_im_w * ti_w;
    wire signed [CB-1:0] prod_i = m_re_w * ti_w + m_im_w * tr_w;

    // memory reads (async; read-old at the current pointer)
    wire signed [WIDTH+TWIDDLE_WIDTH-1:0] v0_re = vline_re[vp];
    wire signed [WIDTH+TWIDDLE_WIDTH-1:0] v0_im = vline_im[vp];
    wire signed [CB-1:0] t1_re = t1line_re[t1p];
    wire signed [CB-1:0] t1_im = t1line_im[t1p];
    wire signed [CB-1:0] t2_re = t2line_re[t2p];
    wire signed [CB-1:0] t2_im = t2line_im[t2p];

    // F4 combine (CW wide)
    localparam integer CW0 = WIDTH + TWIDDLE_WIDTH;   // v0 width
    wire signed [CW-1:0] v0e_re = {{(CW-CW0){v0_re[CW0-1]}}, v0_re};
    wire signed [CW-1:0] v0e_im = {{(CW-CW0){v0_im[CW0-1]}}, v0_im};
    wire signed [CW-1:0] t1e_re = {{(CW-CB){t1_re[CB-1]}}, t1_re};
    wire signed [CW-1:0] t1e_im = {{(CW-CB){t1_im[CB-1]}}, t1_im};
    wire signed [CW-1:0] t2e_re = {{(CW-CB){t2_re[CB-1]}}, t2_re};
    wire signed [CW-1:0] t2e_im = {{(CW-CB){t2_im[CB-1]}}, t2_im};
    wire signed [CW-1:0] t3e_re = {{(CW-CB){prod_r[CB-1]}}, prod_r};
    wire signed [CW-1:0] t3e_im = {{(CW-CB){prod_i[CB-1]}}, prod_i};
    // rot(t2) = -/+j * t2, rot(t3) = -/+j * t3  (js = -1 fwd, +1 inv)
    wire signed [CW-1:0] r2_re = INVERSE ? -t2e_im : t2e_im;
    wire signed [CW-1:0] r2_im = INVERSE ?  t2e_re : -t2e_re;
    wire signed [CW-1:0] r3_re = INVERSE ? -t3e_im : t3e_im;
    wire signed [CW-1:0] r3_im = INVERSE ?  t3e_re : -t3e_re;
    // y0/y2: real rows; y1/y3: the +/-j rows
    wire signed [CW-1:0] y0_r = v0e_re + t1e_re + t2e_re + t3e_re;
    wire signed [CW-1:0] y0_i = v0e_im + t1e_im + t2e_im + t3e_im;
    wire signed [CW-1:0] y2_r = v0e_re + t1e_re - t2e_re - t3e_re;
    wire signed [CW-1:0] y2_i = v0e_im + t1e_im - t2e_im - t3e_im;
    wire signed [CW-1:0] y1_r = v0e_re - t1e_re + r2_re - r3_re;
    wire signed [CW-1:0] y1_i = v0e_im - t1e_im + r2_im - r3_im;
    wire signed [CW-1:0] y3_r = v0e_re - t1e_re - r2_re + r3_re;
    wire signed [CW-1:0] y3_i = v0e_im - t1e_im - r2_im + r3_im;
    wire signed [PW-1:0] q0_r = round_shift_pw({{(PW-CW){y0_r[CW-1]}}, y0_r},
                                               S_TOTAL);
    wire signed [PW-1:0] q0_i = round_shift_pw({{(PW-CW){y0_i[CW-1]}}, y0_i},
                                               S_TOTAL);
    wire signed [PW-1:0] q1_r = round_shift_pw({{(PW-CW){y1_r[CW-1]}}, y1_r},
                                               S_TOTAL);
    wire signed [PW-1:0] q1_i = round_shift_pw({{(PW-CW){y1_i[CW-1]}}, y1_i},
                                               S_TOTAL);
    wire signed [PW-1:0] q2_r = round_shift_pw({{(PW-CW){y2_r[CW-1]}}, y2_r},
                                               S_TOTAL);
    wire signed [PW-1:0] q2_i = round_shift_pw({{(PW-CW){y2_i[CW-1]}}, y2_i},
                                               S_TOTAL);
    wire signed [PW-1:0] q3_r = round_shift_pw({{(PW-CW){y3_r[CW-1]}}, y3_r},
                                               S_TOTAL);
    wire signed [PW-1:0] q3_i = round_shift_pw({{(PW-CW){y3_i[CW-1]}}, y3_i},
                                               S_TOTAL);

    // output mux: y0 at [3H,4H), q1 at [0,H), q2 at [H,2H), q3 at [2H,3H)
    wire signed [WIDTH-1:0] mux_re = phase_a3 ? q0_r[WIDTH-1:0]
                            : (phase_q1 ? q1_re[q1p]
                            : (phase_q2 ? q2_re[q2p] : q3_re[q3p]));
    wire signed [WIDTH-1:0] mux_im = phase_a3 ? q0_i[WIDTH-1:0]
                            : (phase_q1 ? q1_im[q1p]
                            : (phase_q2 ? q2_im[q2p] : q3_im[q3p]));

    always @(posedge clk) begin
        if (rst) begin
            k    <= K_PRELOAD[KW-1:0];
            vp   <= {AW3{1'b0}};
            t1p  <= {AW2{1'b0}};
            t2p  <= {AW1{1'b0}};
            q1p  <= {AW1{1'b0}};
            q2p  <= {AW2{1'b0}};
            q3p  <= {AW3{1'b0}};
            out_re <= {WIDTH{1'b0}};
            out_im <= {WIDTH{1'b0}};
        end else if (ce) begin
            out_re <= mux_re;
            out_im <= mux_im;
            if (phase_a0) begin
                vline_re[vp] <= {{(WIDTH+TWIDDLE_WIDTH-WIDTH){in_re[WIDTH-1]}},
                                 in_re} <<< TWIDDLE_DECIMAL;
                vline_im[vp] <= {{(WIDTH+TWIDDLE_WIDTH-WIDTH){in_im[WIDTH-1]}},
                                 in_im} <<< TWIDDLE_DECIMAL;
            end
            if (phase_a1)
                {t1line_re[t1p], t1line_im[t1p]} <= {prod_r, prod_i};
            if (phase_a2)
                {t2line_re[t2p], t2line_im[t2p]} <= {prod_r, prod_i};
            if (phase_a3) begin
                q1_re[q1p] <= q1_r[WIDTH-1:0];
                q1_im[q1p] <= q1_i[WIDTH-1:0];
                q2_re[q2p] <= q2_r[WIDTH-1:0];
                q2_im[q2p] <= q2_i[WIDTH-1:0];
                q3_re[q3p] <= q3_r[WIDTH-1:0];
                q3_im[q3p] <= q3_i[WIDTH-1:0];
            end
            k   <= k + {{(KW-1){1'b0}}, 1'b1};
            if (vp == 3*DEPTH[AW3-1:0] - 1'b1) vp <= {AW3{1'b0}};
            else vp <= vp + {{(AW3-1){1'b0}}, 1'b1};
            t1p <= t1p + {{(AW2-1){1'b0}}, 1'b1};
            if (t2p == DEPTH[AW1-1:0] - 1'b1) t2p <= {AW1{1'b0}};
            else t2p <= t2p + {{(AW1-1){1'b0}}, 1'b1};
            if (q1p == DEPTH[AW1-1:0] - 1'b1) q1p <= {AW1{1'b0}};
            else q1p <= q1p + {{(AW1-1){1'b0}}, 1'b1};
            q2p <= q2p + {{(AW2-1){1'b0}}, 1'b1};
            if (q3p == 3*DEPTH[AW3-1:0] - 1'b1) q3p <= {AW3{1'b0}};
            else q3p <= q3p + {{(AW3-1){1'b0}}, 1'b1};
        end
    end

endmodule

`default_nettype wire
