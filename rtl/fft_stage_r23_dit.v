// fftgen -- one radix-2^3 DIT stage (S8): the mirror of fft_stage_r23.v
// (multiply-then-combine), bit-reversed input, natural output.
//
// Bit-exact contract: golden._R23DITStage (src/golden.py), validated by
// the DIF->DIT round trip (<=1 LSB, spikes/S7_r23/dit_roundtrip.py) and
// the float-IDFT chain (<1 LSB, spikes/S7_r23/dit_chain_float.py).
//
// Group geometry: period 8G clocks (G = DEPTH = N/8^{m+1}); the member
// at stream phase i*G+g carries Y_bitrev3(i)(g) (the DIF chain's output
// order, bitrev3 = 0,4,2,6,1,5,3,7).  Products AT the arrivals (ONE
// shared complex multiplier, 7/8 duty): the member in window w carries
// DFT-input j = bitrev3(w); c_j = Y_j * T[j*g*8^m] (c_0 = Y_0 << TD).
// Depth-lag lines, depth (7-bitrev3(j))*G for c_j, align all eight at
// window 7 for the F8 DIT combine (3-layer synthesis, ONE lumped shift
// S = TD+SIGMA0+SIGMA1+SIGMA2 at the eight outputs -- the 1/N scaling
// lumped, mirroring the r22 DIT):
//   e_j = c_j+c_{j+4}, f_j = c_j-c_{j+4}            (j = 0..3)
//   f1' = rot45(f1), f2' = js*j*f2, f3' = js*j*rot45(f3)
//   w0 = e0+e2+e1+e3        w4 = e0+e2-e1-e3
//   w2 = e0-e2+js*j(e1-e3)  w6 = e0-e2-js*j(e1-e3)
//   w1 = f0'+f2'+f1'+f3'    w5 = f0'+f2'-f1'-f3'
//   w3 = f0'-f2'+js*j(f1'-f3')  w7 = f0'-f2'-js*j(f1'-f3')
// rot45(x) = round_half_up((x_re - js*x_im)*q8, TD) etc.
// Emission: w0 immediate (out register at window 7), w_m'' (m''=1..7)
// through m''*G-deep queues -> natural order.  Stage latency 7G+1.
//
// ROM layout (7G words per triple, window-ordered): word
// ROM_BASE + (w-1)*G + g holds T[bitrev3(w)*g*8^m], w = 1..7.
// Bring-up style: async distributed ROM/lines/queues, combinational
// product + combine, ONE output register (the timing-tuned sync-read
// pass follows the r23 DIF pattern once bit-exact).  All addresses
// derive from the phase counter k (= the golden's pos mod 8G): every
// write/read pair is separated by exactly the structure depth, so one
// shared index is always phase-correct.

`default_nettype none

module fft_stage_r23_dit #(
    parameter integer DEPTH           = 4,     // G = N/8^{m+1}
    parameter integer WIDTH           = 16,
    parameter integer SIGMA0          = 1,     // sub-stage 3m shift
    parameter integer SIGMA1          = 1,     // sub-stage 3m+1 shift
    parameter integer SIGMA2          = 1,     // sub-stage 3m+2 shift
    parameter integer TWIDDLE_WIDTH   = 18,
    parameter integer TWIDDLE_DECIMAL = 17,
    parameter integer ROM_BASE        = 0,     // slice base (7G per triple)
    parameter integer NPTS            = 128,   // N (full canonical table)
    parameter integer INVERSE         = 0,
    parameter integer Q8              = 92682, // round(sqrt2/2 * 2^TD)
    parameter [15:0] K_PRELOAD        = 16'h0,
    parameter TWIDDLE_FILE            = "fft_tw_r23_dit_t0.mem"
)(
    input  wire             clk,
    input  wire             ce,
    input  wire             rst,
    input  wire signed [WIDTH-1:0] in_re,
    input  wire signed [WIDTH-1:0] in_im,
    output reg  signed [WIDTH-1:0] out_re,
    output reg  signed [WIDTH-1:0] out_im
);

    localparam integer G  = DEPTH;
    localparam integer IW = WIDTH;
    localparam integer TW = TWIDDLE_WIDTH;
    localparam integer TD = TWIDDLE_DECIMAL;
    localparam integer PB = IW + TW + 2;             // product line width
    localparam integer CW = PB + 4;                  // combine width
    localparam integer S_TOT = TD + SIGMA0 + SIGMA1 + SIGMA2;
    localparam integer QW = CW - S_TOT;              // queue word width
    localparam integer KW = (8*G > 1) ? $clog2(8*G) : 1;
    localparam integer GWL = (G > 1) ? $clog2(G) : 0;
    // js = +1 inverse, -1 forward (the omega_8 kernel direction)
    localparam integer JS = (INVERSE != 0) ? 1 : -1;

    // bitrev3 of the window: window w carries DFT-input BR[w]
    function [2:0] br_of;
        input [2:0] w;
        begin
            case (w)
                3'd0: br_of = 3'd0;
                3'd1: br_of = 3'd4;
                3'd2: br_of = 3'd2;
                3'd3: br_of = 3'd6;
                3'd4: br_of = 3'd1;
                3'd5: br_of = 3'd5;
                3'd6: br_of = 3'd3;
                default: br_of = 3'd7;
            endcase
        end
    endfunction

    // ---------------- product lines (c_0..c_6; c_7 fresh) -------------
    // depth_j = (7 - bitrev3(j)) * G
    localparam integer D0 = 7*G, D1 = 3*G, D2 = 5*G, D3 = 1*G,
                       D4 = 6*G, D5 = 2*G, D6 = 4*G;
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl0_re [0:D0-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl0_im [0:D0-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl1_re [0:D1-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl1_im [0:D1-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl2_re [0:D2-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl2_im [0:D2-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl3_re [0:D3-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl3_im [0:D3-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl4_re [0:D4-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl4_im [0:D4-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl5_re [0:D5-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl5_im [0:D5-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl6_re [0:D6-1];
    (* ram_style = "distributed" *)
    reg signed [PB-1:0] pl6_im [0:D6-1];

    // ---------------- output queues (w_1..w_7, depth m''*G) -----------
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q1_re [0:G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q1_im [0:G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q2_re [0:2*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q2_im [0:2*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q3_re [0:3*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q3_im [0:3*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q4_re [0:4*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q4_im [0:4*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q5_re [0:5*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q5_im [0:5*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q6_re [0:6*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q6_im [0:6*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q7_re [0:7*G-1];
    (* ram_style = "distributed" *)
    reg signed [QW-1:0] q7_im [0:7*G-1];

    integer _i;
    initial begin
        for (_i = 0; _i < D0; _i = _i + 1) begin
            pl0_re[_i] = {PB{1'b0}}; pl0_im[_i] = {PB{1'b0}}; end
        for (_i = 0; _i < D1; _i = _i + 1) begin
            pl1_re[_i] = {PB{1'b0}}; pl1_im[_i] = {PB{1'b0}}; end
        for (_i = 0; _i < D2; _i = _i + 1) begin
            pl2_re[_i] = {PB{1'b0}}; pl2_im[_i] = {PB{1'b0}}; end
        for (_i = 0; _i < D3; _i = _i + 1) begin
            pl3_re[_i] = {PB{1'b0}}; pl3_im[_i] = {PB{1'b0}}; end
        for (_i = 0; _i < D4; _i = _i + 1) begin
            pl4_re[_i] = {PB{1'b0}}; pl4_im[_i] = {PB{1'b0}}; end
        for (_i = 0; _i < D5; _i = _i + 1) begin
            pl5_re[_i] = {PB{1'b0}}; pl5_im[_i] = {PB{1'b0}}; end
        for (_i = 0; _i < D6; _i = _i + 1) begin
            pl6_re[_i] = {PB{1'b0}}; pl6_im[_i] = {PB{1'b0}}; end
        for (_i = 0; _i < G; _i = _i + 1) begin
            q1_re[_i] = {QW{1'b0}}; q1_im[_i] = {QW{1'b0}}; end
        for (_i = 0; _i < 2*G; _i = _i + 1) begin
            q2_re[_i] = {QW{1'b0}}; q2_im[_i] = {QW{1'b0}}; end
        for (_i = 0; _i < 3*G; _i = _i + 1) begin
            q3_re[_i] = {QW{1'b0}}; q3_im[_i] = {QW{1'b0}}; end
        for (_i = 0; _i < 4*G; _i = _i + 1) begin
            q4_re[_i] = {QW{1'b0}}; q4_im[_i] = {QW{1'b0}}; end
        for (_i = 0; _i < 5*G; _i = _i + 1) begin
            q5_re[_i] = {QW{1'b0}}; q5_im[_i] = {QW{1'b0}}; end
        for (_i = 0; _i < 6*G; _i = _i + 1) begin
            q6_re[_i] = {QW{1'b0}}; q6_im[_i] = {QW{1'b0}}; end
        for (_i = 0; _i < 7*G; _i = _i + 1) begin
            q7_re[_i] = {QW{1'b0}}; q7_im[_i] = {QW{1'b0}}; end
    end

    // ---------------- phase counter -----------------------------------
    // k = the golden's local phase (pos mod 8G), preloaded by K_PRELOAD;
    // every memory address in the stage derives from k
    reg [KW-1:0] k;
    wire [2:0]  w3 = k[GWL +: 3];                    // window index
    wire [31:0] g_p = k % G;                          // group phase (k mod G)
    wire phase7 = (w3 == 3'd7);

    // round-half-up arithmetic right shift (the golden's round_shift)
    function signed [CW-1:0] rsh_cw;
        input signed [CW+TW-1:0] v;
        input integer sh;
        begin
            if (sh <= 0)
                rsh_cw = {{(CW){v[CW+TW-1]}}, v[CW-1:0]};
            else
                rsh_cw = (v + ($signed({{(CW+TW){1'b0}}, 1'b1})
                                <<< (sh-1))) >>> sh;
        end
    endfunction

    // per-line / per-queue addresses: FREE-RUNNING per-depth counters
    // (the clock count mod m*G) -- NOT k mod depth: depths like 3G/5G/7G
    // do not divide the 8G counter period, so k-based slots would
    // diverge across periods (the golden indexes by its call count).
    // c_m wraps at m*G; line j (depth (7-BR[j])*G) uses c_{7-BR[j]},
    // queue m'' uses c_{m''}.
    localparam integer A1W = (G > 1) ? $clog2(G) : 1;
    localparam integer A2W = (2*G > 1) ? $clog2(2*G) : 1;
    localparam integer A3W = (3*G > 1) ? $clog2(3*G) : 1;
    localparam integer A4W = (4*G > 1) ? $clog2(4*G) : 1;
    localparam integer A5W = (5*G > 1) ? $clog2(5*G) : 1;
    localparam integer A6W = (6*G > 1) ? $clog2(6*G) : 1;
    localparam integer A7W = (7*G > 1) ? $clog2(7*G) : 1;
    reg [A1W-1:0] pc1;
    reg [A2W-1:0] pc2;
    reg [A3W-1:0] pc3;
    reg [A4W-1:0] pc4;
    reg [A5W-1:0] pc5;
    reg [A6W-1:0] pc6;
    reg [A7W-1:0] pc7;
    wire [A1W-1:0] a3  = pc1;   // line 3 (depth G)
    wire [A5W-1:0] a2  = pc5;   // line 2 (depth 5G)
    wire [A2W-1:0] a5  = pc2;   // line 5 (depth 2G)
    wire [A4W-1:0] a6  = pc4;   // line 6 (depth 4G)
    wire [A3W-1:0] a1  = pc3;   // line 1 (depth 3G)
    wire [A6W-1:0] a4  = pc6;   // line 4 (depth 6G)
    wire [A7W-1:0] a0  = pc7;   // line 0 (depth 7G)

    // ---------------- twiddle ROM (window-ordered slices) -------------
    localparam integer ROMW = (NPTS > 1) ? $clog2(NPTS) : 1;
    (* ram_style = "distributed" *)
    reg signed [TW*2-1:0] tw_rom [0:NPTS-1];   // shared file, ROM_BASE slice
    initial $readmemh(TWIDDLE_FILE, tw_rom);
    // addr = ROM_BASE + (w-1)*G + g for w >= 1 (w = 0 never multiplies;
    // its addr is clamped in-range and the result is unused)
    wire [31:0] woff = (w3 == 3'd0) ? 32'd0 : (w3 - 3'd1);
    wire [ROMW-1:0] rom_addr = ROM_BASE[ROMW-1:0] + (woff * G)
                               + g_p;
    wire signed [TW-1:0] tr_w = tw_rom[rom_addr][TW*2-1:TW];
    wire signed [TW-1:0] ti_w = tw_rom[rom_addr][TW-1:0];

    // ---------------- product (ONE shared complex multiplier) ---------
    wire signed [PB-1:0] m_re_w = {{(PB-IW){in_re[IW-1]}}, in_re};
    wire signed [PB-1:0] m_im_w = {{(PB-IW){in_im[IW-1]}}, in_im};
    wire signed [PB-1:0] tr_e = {{(PB-TW){tr_w[TW-1]}}, tr_w};
    wire signed [PB-1:0] ti_e = {{(PB-TW){ti_w[TW-1]}}, ti_w};
    // exact: the true product fits in IW+TW+1 <= PB bits, so the PB-bit
    // evaluation (mod 2^PB) is bit-exact
    wire signed [PB-1:0] prod_r = m_re_w * tr_e - m_im_w * ti_e;
    wire signed [PB-1:0] prod_i = m_re_w * ti_e + m_im_w * tr_e;
    // c_0 = Y_0 << TD (the unmultiplied member, product-scale aligned)
    wire signed [PB-1:0] c0_r = m_re_w <<< TD;
    wire signed [PB-1:0] c0_i = m_im_w <<< TD;

    // ---------------- line reads (async, addr = k mod depth) ----------
    wire signed [PB-1:0] c0_re = pl0_re[a0];
    wire signed [PB-1:0] c0_im = pl0_im[a0];
    wire signed [PB-1:0] c1_re = pl1_re[a1];
    wire signed [PB-1:0] c1_im = pl1_im[a1];
    wire signed [PB-1:0] c2_re = pl2_re[a2];
    wire signed [PB-1:0] c2_im = pl2_im[a2];
    wire signed [PB-1:0] c3_re = pl3_re[a3];
    wire signed [PB-1:0] c3_im = pl3_im[a3];
    wire signed [PB-1:0] c4_re = pl4_re[a4];
    wire signed [PB-1:0] c4_im = pl4_im[a4];
    wire signed [PB-1:0] c5_re = pl5_re[a5];
    wire signed [PB-1:0] c5_im = pl5_im[a5];
    wire signed [PB-1:0] c6_re = pl6_re[a6];
    wire signed [PB-1:0] c6_im = pl6_im[a6];

    // ---------------- F8 DIT combine (combinational) ------------------
    // CW extensions
    wire signed [CW-1:0] x0_re = {{(CW-PB){c0_re[PB-1]}}, c0_re};
    wire signed [CW-1:0] x0_im = {{(CW-PB){c0_im[PB-1]}}, c0_im};
    wire signed [CW-1:0] x1_re = {{(CW-PB){c1_re[PB-1]}}, c1_re};
    wire signed [CW-1:0] x1_im = {{(CW-PB){c1_im[PB-1]}}, c1_im};
    wire signed [CW-1:0] x2_re = {{(CW-PB){c2_re[PB-1]}}, c2_re};
    wire signed [CW-1:0] x2_im = {{(CW-PB){c2_im[PB-1]}}, c2_im};
    wire signed [CW-1:0] x3_re = {{(CW-PB){c3_re[PB-1]}}, c3_re};
    wire signed [CW-1:0] x3_im = {{(CW-PB){c3_im[PB-1]}}, c3_im};
    wire signed [CW-1:0] x4_re = {{(CW-PB){c4_re[PB-1]}}, c4_re};
    wire signed [CW-1:0] x4_im = {{(CW-PB){c4_im[PB-1]}}, c4_im};
    wire signed [CW-1:0] x5_re = {{(CW-PB){c5_re[PB-1]}}, c5_re};
    wire signed [CW-1:0] x5_im = {{(CW-PB){c5_im[PB-1]}}, c5_im};
    wire signed [CW-1:0] x6_re = {{(CW-PB){c6_re[PB-1]}}, c6_re};
    wire signed [CW-1:0] x6_im = {{(CW-PB){c6_im[PB-1]}}, c6_im};
    wire signed [CW-1:0] x7_re = {{(CW-PB){prod_r[PB-1]}}, prod_r};
    wire signed [CW-1:0] x7_im = {{(CW-PB){prod_i[PB-1]}}, prod_i};

    // layer 1: e_j = c_j + c_{j+4}, f_j = c_j - c_{j+4}  (j = 0..3)
    wire signed [CW-1:0] e0_re = x0_re + x4_re, e0_im = x0_im + x4_im;
    wire signed [CW-1:0] e1_re = x1_re + x5_re, e1_im = x1_im + x5_im;
    wire signed [CW-1:0] e2_re = x2_re + x6_re, e2_im = x2_im + x6_im;
    wire signed [CW-1:0] e3_re = x3_re + x7_re, e3_im = x3_im + x7_im;
    wire signed [CW-1:0] f0_re = x0_re - x4_re, f0_im = x0_im - x4_im;
    wire signed [CW-1:0] f1_re = x1_re - x5_re, f1_im = x1_im - x5_im;
    wire signed [CW-1:0] f2_re = x2_re - x6_re, f2_im = x2_im - x6_im;
    wire signed [CW-1:0] f3_re = x3_re - x7_re, f3_im = x3_im - x7_im;

    // rot45(z) = round_half_up((z_re - js*z_im)*q8, TD) etc. -- the only
    // two rotates (f1, f3), the exact mirror of the DIF's rot pipes
    wire signed [CW-1:0] r1a_re = (JS >= 0) ? (f1_re - f1_im)
                                            : (f1_re + f1_im);
    wire signed [CW-1:0] r1a_im = (JS >= 0) ? (f1_im + f1_re)
                                            : (f1_im - f1_re);
    wire signed [CW-1:0] r3a_re = (JS >= 0) ? (f3_re - f3_im)
                                            : (f3_re + f3_im);
    wire signed [CW-1:0] r3a_im = (JS >= 0) ? (f3_im + f3_re)
                                            : (f3_im - f3_re);
    wire signed [CW+TW-1:0] r1m_re = r1a_re * Q8;
    wire signed [CW+TW-1:0] r1m_im = r1a_im * Q8;
    wire signed [CW+TW-1:0] r3m_re = r3a_re * Q8;
    wire signed [CW+TW-1:0] r3m_im = r3a_im * Q8;
    wire signed [CW-1:0] r1_re = rsh_cw(r1m_re, TD);
    wire signed [CW-1:0] r1_im = rsh_cw(r1m_im, TD);
    wire signed [CW-1:0] r3_re = rsh_cw(r3m_re, TD);
    wire signed [CW-1:0] r3_im = rsh_cw(r3m_im, TD);
    // f2' = js*j*f2, f3'' = js*j*f3'
    wire signed [CW-1:0] f2j_re = (JS >= 0) ? -f2_im : f2_im;
    wire signed [CW-1:0] f2j_im = (JS >= 0) ?  f2_re : -f2_re;
    wire signed [CW-1:0] f3j_re = (JS >= 0) ? -r3_im : r3_im;
    wire signed [CW-1:0] f3j_im = (JS >= 0) ?  r3_re : -r3_re;
    wire signed [CW-1:0] fp0_re = f0_re, fp0_im = f0_im;
    wire signed [CW-1:0] fp1_re = r1_re, fp1_im = r1_im;
    wire signed [CW-1:0] fp2_re = f2j_re, fp2_im = f2j_im;
    wire signed [CW-1:0] fp3_re = f3j_re, fp3_im = f3j_im;

    // layer 2 (two DFT-4 syntheses; omega_4 = js*j)
    wire signed [CW-1:0] A_re = e0_re + e2_re, A_im = e0_im + e2_im;
    wire signed [CW-1:0] B_re = e0_re - e2_re, B_im = e0_im - e2_im;
    wire signed [CW-1:0] C_re = e1_re + e3_re, C_im = e1_im + e3_im;
    wire signed [CW-1:0] Dm_re = e1_re - e3_re, Dm_im = e1_im - e3_im;
    wire signed [CW-1:0] E_re = fp0_re + fp2_re, E_im = fp0_im + fp2_im;
    wire signed [CW-1:0] F_re = fp0_re - fp2_re, F_im = fp0_im - fp2_im;
    wire signed [CW-1:0] Gm_re = fp1_re + fp3_re, Gm_im = fp1_im + fp3_im;
    wire signed [CW-1:0] H_re = fp1_re - fp3_re, H_im = fp1_im - fp3_im;
    wire signed [CW-1:0] jmD_re = (JS >= 0) ? -Dm_im : Dm_im;
    wire signed [CW-1:0] jmD_im = (JS >= 0) ?  Dm_re : -Dm_re;
    wire signed [CW-1:0] jmH_re = (JS >= 0) ? -H_im : H_im;
    wire signed [CW-1:0] jmH_im = (JS >= 0) ?  H_re : -H_re;

    // layer 3: the eight outputs, ONE lumped shift S_TOT
    wire signed [CW-1:0] w0_r = rsh_cw(A_re + C_re, S_TOT);
    wire signed [CW-1:0] w0_i = rsh_cw(A_im + C_im, S_TOT);
    wire signed [CW-1:0] w4_r = rsh_cw(A_re - C_re, S_TOT);
    wire signed [CW-1:0] w4_i = rsh_cw(A_im - C_im, S_TOT);
    wire signed [CW-1:0] w2_r = rsh_cw(B_re + jmD_re, S_TOT);
    wire signed [CW-1:0] w2_i = rsh_cw(B_im + jmD_im, S_TOT);
    wire signed [CW-1:0] w6_r = rsh_cw(B_re - jmD_re, S_TOT);
    wire signed [CW-1:0] w6_i = rsh_cw(B_im - jmD_im, S_TOT);
    wire signed [CW-1:0] w1_r = rsh_cw(E_re + Gm_re, S_TOT);
    wire signed [CW-1:0] w1_i = rsh_cw(E_im + Gm_im, S_TOT);
    wire signed [CW-1:0] w5_r = rsh_cw(E_re - Gm_re, S_TOT);
    wire signed [CW-1:0] w5_i = rsh_cw(E_im - Gm_im, S_TOT);
    wire signed [CW-1:0] w3_r = rsh_cw(F_re + jmH_re, S_TOT);
    wire signed [CW-1:0] w3_i = rsh_cw(F_im + jmH_im, S_TOT);
    wire signed [CW-1:0] w7_r = rsh_cw(F_re - jmH_re, S_TOT);
    wire signed [CW-1:0] w7_i = rsh_cw(F_im - jmH_im, S_TOT);

    // ---------------- queue writes (window 7) + reads -----------------
    // the queue m'' drains at window m''-1 through the SAME free-running
    // pointer it was written with (the write->read distance = m''*G = the
    // depth, so one shared pointer is always phase-correct)
    wire [A2W-1:0] gq2 = pc2;
    wire [A3W-1:0] gq3 = pc3;
    wire [A4W-1:0] gq4 = pc4;
    wire [A5W-1:0] gq5 = pc5;
    wire [A6W-1:0] gq6 = pc6;
    wire [A7W-1:0] gq7 = pc7;

    // output mux: w0 immediate at window 7, else the w_{w+1} queue
    // (read at window w = the queue's m''-1, written m''*G clocks ago)
    reg signed [QW-1:0] mux_re, mux_im;
    always @* begin
        case (w3)
            3'd0: begin mux_re = q1_re[pc1]; mux_im = q1_im[pc1]; end
            3'd1: begin mux_re = q2_re[gq2]; mux_im = q2_im[gq2]; end
            3'd2: begin mux_re = q3_re[gq3]; mux_im = q3_im[gq3]; end
            3'd3: begin mux_re = q4_re[gq4]; mux_im = q4_im[gq4]; end
            3'd4: begin mux_re = q5_re[gq5]; mux_im = q5_im[gq5]; end
            3'd5: begin mux_re = q6_re[gq6]; mux_im = q6_im[gq6]; end
            default: begin mux_re = q7_re[gq7]; mux_im = q7_im[gq7]; end
        endcase
    end

    // w values truncated to the queue word width (lossless: the scaling
    // schedule keeps |w| <= 2^(W+2-sum(sigma)) << 2^QW)
    wire signed [QW-1:0] w0_q = w0_r[QW-1:0];
    wire signed [QW-1:0] w0_iq = w0_i[QW-1:0];
    wire signed [QW-1:0] w1_q = w1_r[QW-1:0];
    wire signed [QW-1:0] w2_q = w2_r[QW-1:0];
    wire signed [QW-1:0] w3_q = w3_r[QW-1:0];
    wire signed [QW-1:0] w4_q = w4_r[QW-1:0];
    wire signed [QW-1:0] w5_q = w5_r[QW-1:0];
    wire signed [QW-1:0] w6_q = w6_r[QW-1:0];
    wire signed [QW-1:0] w7_q = w7_r[QW-1:0];

    always @(posedge clk) begin
        if (rst) begin
            k      <= K_PRELOAD[KW-1:0];
            pc1    <= {A1W{1'b0}};
            pc2    <= {A2W{1'b0}};
            pc3    <= {A3W{1'b0}};
            pc4    <= {A4W{1'b0}};
            pc5    <= {A5W{1'b0}};
            pc6    <= {A6W{1'b0}};
            pc7    <= {A7W{1'b0}};
            out_re <= {IW{1'b0}};
            out_im <= {IW{1'b0}};
        end else if (ce) begin
            // output register: w0 at window 7, else the drained queue
            out_re <= phase7 ? w0_q[IW-1:0] : mux_re[IW-1:0];
            out_im <= phase7 ? w0_iq[IW-1:0] : mux_im[IW-1:0];
            // product-line writes at the arrivals (window w -> line BR[w];
            // window 0 = the unmultiplied member << TD, window 7 = fresh)
            case (w3)
                3'd0: begin
                    pl0_re[a0] <= c0_r;
                    pl0_im[a0] <= c0_i;
                end
                3'd1: begin
                    pl4_re[a4] <= prod_r;
                    pl4_im[a4] <= prod_i;
                end
                3'd2: begin
                    pl2_re[a2] <= prod_r;
                    pl2_im[a2] <= prod_i;
                end
                3'd3: begin
                    pl6_re[a6] <= prod_r;
                    pl6_im[a6] <= prod_i;
                end
                3'd4: begin
                    pl1_re[a1] <= prod_r;
                    pl1_im[a1] <= prod_i;
                end
                3'd5: begin
                    pl5_re[a5] <= prod_r;
                    pl5_im[a5] <= prod_i;
                end
                3'd6: begin
                    pl3_re[a3] <= prod_r;
                    pl3_im[a3] <= prod_i;
                end
                default: begin
                    // window 7: c_7 is consumed fresh by the combine
                end
            endcase
            // output-queue writes at the combine window
            if (phase7) begin
                q1_re[pc1] <= w1_q;
                q1_im[pc1] <= w1_i[QW-1:0];
                q2_re[gq2] <= w2_q;
                q2_im[gq2] <= w2_i[QW-1:0];
                q3_re[gq3] <= w3_q;
                q3_im[gq3] <= w3_i[QW-1:0];
                q4_re[gq4] <= w4_q;
                q4_im[gq4] <= w4_i[QW-1:0];
                q5_re[gq5] <= w5_q;
                q5_im[gq5] <= w5_i[QW-1:0];
                q6_re[gq6] <= w6_q;
                q6_im[gq6] <= w6_i[QW-1:0];
                q7_re[gq7] <= w7_q;
                q7_im[gq7] <= w7_i[QW-1:0];
            end
            k <= k + {{(KW-1){1'b0}}, 1'b1};
            // free-running per-depth pointers (reset to 0; the golden's
            // call counter starts at 0 too, so they align by construction
            // and stay independent of K_PRELOAD)
            pc1 <= (pc1 == (G-1)) ? {A1W{1'b0}}
                                  : pc1 + {{(A1W-1){1'b0}}, 1'b1};
            pc2 <= (pc2 == (2*G-1)) ? {A2W{1'b0}}
                                        : pc2 + {{(A2W-1){1'b0}}, 1'b1};
            pc3 <= (pc3 == (3*G-1)) ? {A3W{1'b0}}
                                        : pc3 + {{(A3W-1){1'b0}}, 1'b1};
            pc4 <= (pc4 == (4*G-1)) ? {A4W{1'b0}}
                                        : pc4 + {{(A4W-1){1'b0}}, 1'b1};
            pc5 <= (pc5 == (5*G-1)) ? {A5W{1'b0}}
                                        : pc5 + {{(A5W-1){1'b0}}, 1'b1};
            pc6 <= (pc6 == (6*G-1)) ? {A6W{1'b0}}
                                        : pc6 + {{(A6W-1){1'b0}}, 1'b1};
            pc7 <= (pc7 == (7*G-1)) ? {A7W{1'b0}}
                                        : pc7 + {{(A7W-1){1'b0}}, 1'b1};
        end
    end

endmodule

`default_nettype wire
