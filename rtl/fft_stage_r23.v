// fftgen -- one radix-2^3 DIF stage (S7): the DIF stage triple
// (3m, 3m+1, 3m+2) merged into one 8-sample group with ONE shared
// complex multiplier (7 products per 8-clock group, staggered) and TWO
// fabric 45-degree rotate units (the W8 kernel constants).
//
// Bit-exact contract: golden._R23DIFStage (src/golden.py), derived and
// verified in spikes/S7_r23/ (notes.md has the schedule and the layer
// table; rot45_probe/ proves the ternary rotate trees at 2 ns).
//
// Group geometry: period 8G clocks (G = DEPTH = N/8^{m+1}); group g's
// members a_i arrive at phases i*G+g. Kernel outputs y_k:
//   y0        sum path (no multiply)
//   y1/y5     bm +- (r1 + js*j*r3),  bm = d0 + js*j*d2
//   y3/y7     bp +- (r3 + js*j*r1),  bp = d0 - js*j*d2
//   y2/y6     q0 +- js*j*q1
//   y4        p0 - p1  (p1 fresh in the slot window)
//   r1 = rot45(d1), r3 = rot45(d3): the ONLY two rotates; rot45(x) =
//   round_half_up((x_re - js*x_im) * q8, TD) etc., q8 = round(sqrt2/2*2^TD).
// Output position i*G+g carries y_bitrev3(i)(g), bitrev3=0,4,2,6,1,5,3,7.
//
// Memory style: MEM_STYLE ("block" for G > 32, "distributed" else -- the
// wrapper sets it). Every ring read is a TWO-STAGE path: r1 (pulled into
// the BRAM DOA/DOB, or the free CLB FF after a LUTRAM read) then r2 (a
// fabric stage) -- the RTL PROVIDES the register stages the tools can
// absorb into dsp/bram primitives; the +1 read latency shifts H to 7
// (y4: 8), which the golden model tolerates by construction.
// Pipeline (the model slot phase w maps to the pfifo write at w+H):
//   w-1   twiddle addr (registered decode for w+2's BREG)
//   w     ring addrs presented (sync read); L0 capture r1 at end of w
//   w+1   r2 (fabric stage after the RAM output reg)
//   w+2   L1 comb: class combines c1..c7, second-level p/q/bm/bp/y0,
//         butterflies sA/dA; AREG/BREG capture; k2-gated ring writes
//   w+3   im-path products (MREG) + re-operand freeze
//   w+4   re-path products (MREG) + C-port regs (CREG)
//   w+5   ALU combine (PREG)
//   w+6   per-class fused shift staging (shift_p)
//   w+7   pfifo write + emission mux (k7-gated; y4 at w+8 via k8)
// Product slots (model phases, one cmul slot/clk, [2G,3G) idle):
//   [0,G) y2  [G,2G) y6  [3G,4G) y1  [4G,5G) y5  [5G,6G) y3
//   [6G,7G) y7  [7G,8G) y4
// Rotate timing (model phases): unit B takes dA_3 at 7G+g; unit A reads
// ringA_d1[(g+3)%G] at 10G-3+g; the rot tree is 3 registered stages and
// the ringR writes land at 7G+3+g (unit B; the +3 pipeline hop shifts
// the phase->group map by 3) and 10G+g (unit A) -- G==1 uses the L1-comb
// rot input (+3); G>1 the registered input (+4, still 2 clocks before
// the first d-class slot read at 11G+g).
//
// Q8's set-bit tap positions (<= 7) are extracted by a constant
// function; configs whose q8 popcount differs from 7 are rejected
// (the generator constrains TWIDDLE_DECIMAL for r23).

`default_nettype none

module fft_stage_r23 #(
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
    parameter MEM_STYLE               = "block",  // (legacy, unused)
    parameter integer USE_URAM        = 0,     // 1: the big rings + tw ROM
                                               //    map to URAM288 instead
                                               //    of BRAM (same 1-cycle
                                               //    registered-read contract;
                                               //    doc/uram_study.md)
    parameter [15:0] K_PRELOAD        = 16'h0,
    parameter TWIDDLE_FILE            = "fft_twiddles_r23.mem"
)(
    input  wire             clk,
    input  wire             ce,
    input  wire             rst,
    input  wire signed [WIDTH-1:0] in_re,
    input  wire signed [WIDTH-1:0] in_im,
    output reg  signed [WIDTH-1:0] out_re,
    output reg  signed [WIDTH-1:0] out_im
);

    localparam integer G   = DEPTH;
    localparam integer IW  = WIDTH;
    localparam integer BW  = WIDTH + 1;
    localparam integer CB  = WIDTH + 2;              // sA/dA/p/q/bm/r width
    localparam integer OB  = WIDTH + 4;              // product operand width
    localparam integer TW  = TWIDDLE_WIDTH;
    localparam integer TD  = TWIDDLE_DECIMAL;
    localparam integer OW  = CB + 1 + TW;            // rot tree width
    localparam integer MWB = OB + TW + 1;            // product width
    localparam integer PW  = MWB + 2;                // shift staging width
    localparam integer AW  = (4*G > 1) ? $clog2(4*G) : 1;
    localparam integer SW  = (2*G > 1) ? $clog2(2*G) : 1;
    localparam integer GW  = (G   > 1) ? $clog2(G)   : 1;
    localparam integer KW  = (8*G > 1) ? $clog2(8*G) : 1;

    // Q8 tap positions (constant function over the parameter)
    function [4:0] tap_idx;
        input integer n;
        integer b, c;
        begin
            c = 0;
            tap_idx = 5'd0;
            for (b = 0; b < TW + 2; b = b + 1) begin
                if (Q8[b] && (c == n)) begin
                    tap_idx = b[4:0];
                end
                if (Q8[b]) begin
                    c = c + 1;
                end
            end
        end
    endfunction
    function integer popcnt_q;
        input integer v;
        integer b, c;
        begin
            c = 0;
            for (b = 0; b < TW + 2; b = b + 1) begin
                if (v[b]) begin
                    c = c + 1;
                end
            end
            popcnt_q = c;
        end
    endfunction
    localparam integer Q8_TAPS = popcnt_q(Q8);
    // synthesis translate_off
    initial if (Q8_TAPS != 7) begin
        $error("fft_stage_r23: Q8 popcount %0d != 7 unsupported",
               Q8_TAPS);
    end
    // synthesis translate_on

    // URAM option: the rings and the twiddle ROM all use the 1-cycle
    // registered-read (DOUT) contract, so BRAM and URAM288 are
    // behaviorally interchangeable here (S1 spike: ram_style="ultra"
    // maps the same codings; doc/uram_study.md). USE_URAM=1 hints
    // "ultra" on the big arrays AND on the auto product-FIFO rings
    // (which Vivado otherwise maps to BRAM at large G).
    localparam RING_STYLE = (USE_URAM != 0) ? "ultra" : "block";
    localparam PF_STYLE   = (USE_URAM != 0) ? "ultra" : "auto";
    // ring0 exception: the only 4G-deep array (a 4-high URAM cascade at
    // N=32768) -- its clock-to-out misses the read -> a0_r -> butterfly
    // path (WNS -0.501 / 55 FEP at SG2 N=32768; the G-deep single-URAM
    // rings are fine). Stays in BRAM; ~16 BRAM36 at N=32768.
    localparam RING0_STYLE = "block";
    // ringA_s exception (USE_URAM=1): its 2G-deep URAM read was among
    // the last failing paths at SG2 N=32768 (-0.154 / 40 FEP); keeping
    // it in BRAM leaves only fabric g_r->ROM paths at the edge.
    localparam RINGS_STYLE = "block";
    // the twiddle ROM stays in BRAM/LUTRAM always: URAM288 cannot be
    // initialized (no INIT support), so $readmemh ROMs cannot live
    // there. (URAM clock-to-out would also break the ROM's 1-cycle
    // DOUT -> DSP-BREG contract: WNS -1.55 at N=32768 SG1 -- see
    // doc/uram_study.md)
    localparam TW_STYLE   = "block";

    // ---------------- memories (distributed, async read) --------------
    // ring0 holds raw inputs (IW); every other ring holds CB-wide
    // internal values (the auto schedule keeps them inside CB:
    // |rot(dA)| <= sqrt2 * 2^W < 2^(W+1) = CB range, notes.md).
    (* ram_style = RING0_STYLE *)
    reg signed [IW-1:0] ring0_re   [0:4*G-1];
    (* ram_style = RING0_STYLE *)
    reg signed [IW-1:0] ring0_im   [0:4*G-1];
    (* ram_style = RINGS_STYLE *)
    reg signed [CB-1:0] ringA_s_re [0:2*G-1];
    (* ram_style = RINGS_STYLE *)
    reg signed [CB-1:0] ringA_s_im [0:2*G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] ringA_d0_re [0:G-1];
    reg signed [CB-1:0] ringA_d0_im [0:G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] ringA_d1_re [0:G-1];
    reg signed [CB-1:0] ringA_d1_im [0:G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] ringB_p_re [0:G-1];
    reg signed [CB-1:0] ringB_p_im [0:G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] ringB_q_re [0:G-1];
    reg signed [CB-1:0] ringB_q_im [0:G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] ringB_q1_re [0:G-1];
    reg signed [CB-1:0] ringB_q1_im [0:G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] rbbm_re [0:G-1];
    reg signed [CB-1:0] rbbm_im [0:G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] rbbp_re [0:G-1];
    reg signed [CB-1:0] rbbp_im [0:G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] rr1_re [0:G-1];
    reg signed [CB-1:0] rr1_im [0:G-1];
    (* ram_style = RING_STYLE *)
    reg signed [CB-1:0] rr3_re [0:G-1];
    reg signed [CB-1:0] rr3_im [0:G-1];
    // one G-deep LUTRAM per class (the single 8G-deep array needed the
    // cls_base adder in front of a 128:1 chunk-select read mux and
    // missed 2 ns; per-class arrays are addressed by the raw group and
    // selected by the 3-bit member)
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf1_re [0:G-1];
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf1_im [0:G-1];  // y2
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf2_re [0:G-1];
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf2_im [0:G-1];  // y6
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf3_re [0:G-1];
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf3_im [0:G-1];  // y1
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf4_re [0:G-1];
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf4_im [0:G-1];  // y5
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf5_re [0:G-1];
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf5_im [0:G-1];  // y3
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf6_re [0:G-1];
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf6_im [0:G-1];  // y7
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf7_re [0:G-1];
    (* ram_style = PF_STYLE *)
    reg signed [IW-1:0] pf7_im [0:G-1];  // y4
    integer _i;
    initial begin
        for (_i = 0; _i < 4*G; _i = _i + 1) begin
            ring0_re[_i] = {IW{1'b0}};  ring0_im[_i] = {IW{1'b0}};
        end
        for (_i = 0; _i < 2*G; _i = _i + 1) begin
            ringA_s_re[_i] = {CB{1'b0}}; ringA_s_im[_i] = {CB{1'b0}};
        end
        for (_i = 0; _i < G; _i = _i + 1) begin
            ringA_d0_re[_i] = {CB{1'b0}}; ringA_d0_im[_i] = {CB{1'b0}};
            ringA_d1_re[_i] = {CB{1'b0}}; ringA_d1_im[_i] = {CB{1'b0}};
            ringB_p_re[_i] = {CB{1'b0}};  ringB_p_im[_i] = {CB{1'b0}};
            ringB_q_re[_i] = {CB{1'b0}};  ringB_q_im[_i] = {CB{1'b0}};
            ringB_q1_re[_i] = {CB{1'b0}}; ringB_q1_im[_i] = {CB{1'b0}};
            rbbm_re[_i] = {CB{1'b0}};     rbbm_im[_i] = {CB{1'b0}};
            rbbp_re[_i] = {CB{1'b0}};     rbbp_im[_i] = {CB{1'b0}};
            rr1_re[_i] = {CB{1'b0}};      rr1_im[_i] = {CB{1'b0}};
            rr3_re[_i] = {CB{1'b0}};      rr3_im[_i] = {CB{1'b0}};
        end
        for (_i = 0; _i < G; _i = _i + 1) begin
            pf1_re[_i] = {IW{1'b0}}; pf1_im[_i] = {IW{1'b0}};
            pf2_re[_i] = {IW{1'b0}}; pf2_im[_i] = {IW{1'b0}};
            pf3_re[_i] = {IW{1'b0}}; pf3_im[_i] = {IW{1'b0}};
            pf4_re[_i] = {IW{1'b0}}; pf4_im[_i] = {IW{1'b0}};
            pf5_re[_i] = {IW{1'b0}}; pf5_im[_i] = {IW{1'b0}};
            pf6_re[_i] = {IW{1'b0}}; pf6_im[_i] = {IW{1'b0}};
            pf7_re[_i] = {IW{1'b0}}; pf7_im[_i] = {IW{1'b0}};
        end
    end

    // ---------------- phase counter + delayed chain -------------------
    // k_d at clock c = the model phase of clock c-d (the k1-gated ring
    // writes use k1's group: the data computed during c belongs to the
    // L0 snapshot of phase c-1).
    reg [KW-1:0] k;
    (* shreg_extract = "no" *) reg [KW-1:0] k1, k2, k3, k4, k5, k6,
        k7, k8, k9, k10;

    wire [AW-1:0] a0_addr = k[AW-1:0];
    wire [SW-1:0] as_addr = k[SW-1:0];
    // the ringA_s WRITE addr: the writes fire at w+3, so the golden's
    // group index (slot g / G+g) needs the -3 phase shift
    wire [SW-1:0] as_addr_w = k[SW-1:0] + (2*G-3);
    wire [GW-1:0] g_addr  = (G > 1) ? k[GW-1:0] : {GW{1'b0}};

    // ---------------- class decode (shared functions) -----------------
    // kernel classes by model phase window: 1=y2 2=y6 3=y1 4=y5 5=y3
    // 6=y7 7=y4, 0 = idle ([2G,3G))
    function [2:0] cls_of;
        input [KW-1:0] p;
        begin
            if (p < G)                      cls_of = 3'd1;
            else if (p < 2*G)               cls_of = 3'd2;
            else if (p < 3*G)               cls_of = 3'd0;
            else if (p < 4*G)               cls_of = 3'd3;
            else if (p < 5*G)               cls_of = 3'd4;
            else if (p < 6*G)               cls_of = 3'd5;
            else if (p < 7*G)               cls_of = 3'd6;
            else                            cls_of = 3'd7;
        end
    endfunction
    // pfifo slice base per class (ROM and pfifo share the layout:
    // word ROM_BASE + k*G + g holds T[k*g*8^m], k = 1..7)
    function [KW-1:0] cls_base;
        input [2:0] c;
        begin
            case (c)
                3'd1: cls_base = 0;            // y2
                3'd2: cls_base = G;            // y6
                3'd3: cls_base = 3*G;          // y1
                3'd4: cls_base = 4*G;          // y5
                3'd5: cls_base = 5*G;          // y3
                3'd6: cls_base = 6*G;          // y7
                3'd7: cls_base = 7*G;          // y4
                default: cls_base = 0;
            endcase
        end
    endfunction

    // ---------------- twiddle ROM (BRAM, registered read) -------------
    localparam integer ROMW = (NPTS > 1) ? $clog2(NPTS) : 1;
    (* ram_style = TW_STYLE *)
    reg signed [TW*2-1:0] tw_rom [0:NPTS-1];
    initial $readmemh(TWIDDLE_FILE, tw_rom);
    wire [KW-1:0] k_next = k + {{(KW-1){1'b0}}, 1'b1};
    // the BREG captures tw_dout at w+1, so the addr (issued at w-1)
    // decodes the class of w: cls_of(k at w-1 + 1) -- registered here
    // as addr = f(k_next) one clock ahead of the read
    wire [2:0] cls_next = cls_of(k_next);
    wire [GW-1:0] g_next = (G > 1) ? k_next[GW-1:0] : {GW{1'b0}};
    wire [ROMW-1:0] rom_addr = ROM_BASE[ROMW-1:0]
                             + cls_base(cls_next)
                             + {{(ROMW-GW){1'b0}}, g_next};
    reg [TW*2-1:0] tw_dout;
    reg [TW*2-1:0] tw_h1, tw_h2, tw_h3, tw_h4;  // hold chain to BREGs
    // both BREGs capture at w+4 (the normal AREG moved to w+4 with the
    // registered combs): tw_h4@t = T(t-4) = the capture slot's twiddle
    wire signed [TW-1:0] tr_w = tw_h4[TW*2-1:TW];
    wire signed [TW-1:0] ti_w = tw_h4[TW-1:0];
    wire signed [TW-1:0] tr4_w = tw_h4[TW*2-1:TW];
    wire signed [TW-1:0] ti4_w = tw_h4[TW-1:0];

    // ---------------- round-half-up shifts ----------------------------
    function signed [CB-1:0] rsh_cb;
        input signed [CB-1:0] v;
        input integer sh;
        begin
            if (sh <= 0) begin
                rsh_cb = v;
            end else begin
                rsh_cb = (v + ($signed({{(CB-1){1'b0}}, 1'b1})
                                <<< (sh-1))) >>> sh;
            end
        end
    endfunction
    function signed [PW-1:0] rsh_pw;
        input signed [PW-1:0] v;
        input integer sh;
        begin
            if (sh <= 0) begin
                rsh_pw = v;
            end else begin
                rsh_pw = (v + ($signed({{(PW-1){1'b0}}, 1'b1})
                                <<< (sh-1))) >>> sh;
            end
        end
    endfunction

    wire signed [BW-1:0] x_re = {{(BW-IW){in_re[IW-1]}}, in_re};
    wire signed [BW-1:0] x_im = {{(BW-IW){in_im[IW-1]}}, in_im};

    // ---------------- L0: capture reads + input -----------------------
    reg signed [CB-1:0] a0_r_re,  a0_r_im;     // ring0 read (extended)
    reg signed [CB-1:0] as_r_re,  as_r_im;     // ringA_s read
    reg signed [CB-1:0] ad0_r_re, ad0_r_im;    // ringA_d0 read
    reg signed [CB-1:0] ad1_r_re, ad1_r_im;    // ringA_d1 read (g+3)
    reg signed [CB-1:0] bp_r_re,  bp_r_im;     // ringB_p read
    reg signed [CB-1:0] bq_r_re,  bq_r_im;     // ringB_q read
    reg signed [CB-1:0] bq1_r_re, bq1_r_im;    // ringB_q1 read
    reg signed [CB-1:0] bm_r_re,  bm_r_im;     // ringBB bm read
    reg signed [CB-1:0] bp2_r_re, bp2_r_im;    // ringBB bp read
    reg signed [CB-1:0] r1_r_re,  r1_r_im;     // ringR1 read
    reg signed [CB-1:0] r3_r_re,  r3_r_im;     // ringR3 read (r1)
    reg signed [BW-1:0] x_r1_re,  x_r1_im;     // input (r1)
    // r2: the fabric stage after the RAM output register -- the L1 comb
    // consumes THESE (the tools pull r1 into bram DOA/DOB or keep it as
    // the free CLB FF after a LUTRAM read)
    reg signed [CB-1:0] a0_r2_re,  a0_r2_im;
    reg signed [CB-1:0] as_r2_re,  as_r2_im;
    reg signed [CB-1:0] ad0_r2_re, ad0_r2_im;
    reg signed [CB-1:0] ad1_r2_re, ad1_r2_im;
    reg signed [CB-1:0] bp_r2_re,  bp_r2_im;
    reg signed [CB-1:0] bq_r2_re,  bq_r2_im;
    reg signed [CB-1:0] bq1_r2_re, bq1_r2_im;
    reg signed [CB-1:0] bm_r2_re,  bm_r2_im;
    reg signed [CB-1:0] bp2_r2_re, bp2_r2_im;
    reg signed [CB-1:0] r1_r2_re,  r1_r2_im;
    reg signed [CB-1:0] r3_r2_re,  r3_r2_im;
    reg signed [BW-1:0] x_r2_re,   x_r2_im;
    // r3: the tools absorb r1/r2 into the BRAM's ADDRA + DOA; r3 is
    // the fabric register the L1 comb starts from
    reg signed [CB-1:0] a0_r3_re,  a0_r3_im;
    reg signed [CB-1:0] as_r3_re,  as_r3_im;
    reg signed [CB-1:0] ad0_r3_re, ad0_r3_im;
    reg signed [CB-1:0] ad1_r3_re, ad1_r3_im;
    reg signed [CB-1:0] bp_r3_re,  bp_r3_im;
    reg signed [CB-1:0] bq_r3_re,  bq_r3_im;
    reg signed [CB-1:0] bq1_r3_re, bq1_r3_im;
    reg signed [CB-1:0] bm_r3_re,  bm_r3_im;
    reg signed [CB-1:0] bp2_r3_re, bp2_r3_im;
    reg signed [CB-1:0] r1_r3_re,  r1_r3_im;
    reg signed [CB-1:0] r3_r3_re,  r3_r3_im;
    reg signed [BW-1:0] x_r3_re,   x_r3_im;
    reg signed [CB-1:0] dA_r3_re,  dA_r3_im;   // registered dA (rot B in)
    reg signed [CB-1:0] p1_r_re,   p1_r_im;    // registered p_1 (y4 comb)
    // 4th read stage: the second-level ring writes run at w+4 and read
    // the group-w value with the SAME addr convention as the golden
    reg signed [CB-1:0] as_r4_re,  as_r4_im;
    reg signed [CB-1:0] ad0_r4_re, ad0_r4_im;
    reg signed [CB-1:0] bp_r4_re,  bp_r4_im;
    // the fresh combs delayed one clock: the w+4 combines consume these
    reg signed [CB-1:0] sA_r_re,   sA_r_im;
    reg signed [CB-1:0] jmdA_r_re, jmdA_r_im;  // j-swapped dA (bm/bp)
    // the w6/w7 write data registered one cycle ahead (the combines are
    // register->register; the BRAM DIN gets a direct FF hop), with the
    // write gates/address shifted +1 to consume them -- the ring reads
    // are untouched so every lag grows by exactly 1
    reg signed [CB-1:0] ringB_p_w_re, ringB_p_w_im;
    reg signed [CB-1:0] ringB_q_w_re, ringB_q_w_im;
    reg signed [CB-1:0] rbbm_w_re,   rbbm_w_im;
    reg signed [CB-1:0] rbbp_w_re,   rbbp_w_im;
    reg w6_r, w7_r;
    reg [GW-1:0] g_w4_r;

    // the +4 rotate hop remap: the 3-stage read shifts the
    // phase->group map (the golden's (g+3) + the read latency)
    // +5: the golden's ringR[x] = rot(d1(group x)) (its rotA2 snapshot
    // is taken pre-shift, so the +3 read map and the 3-deep pipe cancel);
    // the RTL's write@2G+x consumes the comb@2G+x-2, whose r3 read was
    // addressed at 2G+x-5 -- the addr constant must be 5
    wire [GW-1:0] g_a3 = (g_addr + 5) % G;
    // sync reads: the L0 captures read the memories DIRECTLY in the
    // clocked block -- the async-read wires forced LUTRAM and
    // blocked the BRAM mapping (the user directive: deep rings
    // in BRAM, with the register stages the tools can absorb)
    wire signed [CB-1:0] rr1_im_w = rr1_im[g_addr];
    wire signed [CB-1:0] rr3_re_w = rr3_re[g_addr];
    wire signed [CB-1:0] rr3_im_w = rr3_im[g_addr];

    // ---------------- L1: butterflies + class combines ----------------
    // butterflies: sA = round(a0+x, s0), dA = a0-x
    wire signed [CB-1:0] sA_re_c = rsh_cb(a0_r3_re + x_r3_re, SIGMA0);
    wire signed [CB-1:0] sA_im_c = rsh_cb(a0_r3_im + x_r3_im, SIGMA0);
    // the dA subtract is registered one stage earlier (off the r2
    // registers): dA_f[C] = a0_r2[C-1] - x_r2[C-1] = a0_r3[C] - x_r3[C],
    // bit-identical to the old comb dA_re_c[C], but every consumer now
    // reads a register (the 18-bit subtract's 3x CARRY8 chain into the
    // ringA_d0/d1 BRAM write data and the jmdA capture missed 2 ns)
    reg signed [CB-1:0] dA_f_re, dA_f_im;

    // OB extensions of the L0 snapshot
    wire signed [OB-1:0] q0_re  = {{(OB-CB){bq_r3_re[CB-1]}},  bq_r3_re};
    wire signed [OB-1:0] q0_im  = {{(OB-CB){bq_r3_im[CB-1]}},  bq_r3_im};
    wire signed [OB-1:0] q1_re  = {{(OB-CB){bq1_r3_re[CB-1]}}, bq1_r3_re};
    wire signed [OB-1:0] q1_im  = {{(OB-CB){bq1_r3_im[CB-1]}}, bq1_r3_im};
    wire signed [OB-1:0] bm_re  = {{(OB-CB){bm_r3_re[CB-1]}},  bm_r3_re};
    wire signed [OB-1:0] bm_im  = {{(OB-CB){bm_r3_im[CB-1]}},  bm_r3_im};
    wire signed [OB-1:0] bp2_re = {{(OB-CB){bp2_r3_re[CB-1]}}, bp2_r3_re};
    wire signed [OB-1:0] bp2_im = {{(OB-CB){bp2_r3_im[CB-1]}}, bp2_r3_im};
    wire signed [OB-1:0] r1e_re = {{(OB-CB){r1_r3_re[CB-1]}},  r1_r3_re};
    wire signed [OB-1:0] r1e_im = {{(OB-CB){r1_r3_im[CB-1]}},  r1_r3_im};
    wire signed [OB-1:0] r3e_re = {{(OB-CB){r3_r3_re[CB-1]}},  r3_r3_re};
    wire signed [OB-1:0] r3e_im = {{(OB-CB){r3_r3_im[CB-1]}},  r3_r3_im};
    wire signed [OB-1:0] p0e_re = {{(OB-CB){bp_r4_re[CB-1]}},  bp_r4_re};
    wire signed [OB-1:0] p0e_im = {{(OB-CB){bp_r4_im[CB-1]}},  bp_r4_im};

    // js*j fold: jm(z)_re = -js*z_im, jm(z)_im = js*z_re
    // (INVERSE=0: js=-1 -> jm_re = z_im, jm_im = -z_re)
    wire signed [OB-1:0] jq1_re = INVERSE ? -q1_im : q1_im;
    wire signed [OB-1:0] jq1_im = INVERSE ? q1_re : -q1_re;
    wire signed [OB-1:0] jr1_re = INVERSE ? -r1e_im : r1e_im;
    wire signed [OB-1:0] jr1_im = INVERSE ? r1e_re : -r1e_re;
    wire signed [OB-1:0] jr3_re = INVERSE ? -r3e_im : r3e_im;
    wire signed [OB-1:0] jr3_im = INVERSE ? r3e_re : -r3e_re;

    // class combines (L1 comb, OB domain)
    wire signed [OB-1:0] c2_re = q0_re + jq1_re;   // y2: q0 + js*j*q1
    wire signed [OB-1:0] c2_im = q0_im + jq1_im;
    wire signed [OB-1:0] c6_re = q0_re - jq1_re;   // y6
    wire signed [OB-1:0] c6_im = q0_im - jq1_im;
    wire signed [OB-1:0] c1_re = bm_re + r1e_re + jr3_re;  // y1
    wire signed [OB-1:0] c1_im = bm_im + r1e_im + jr3_im;
    wire signed [OB-1:0] c5_re = bm_re - r1e_re - jr3_re;  // y5
    wire signed [OB-1:0] c5_im = bm_im - r1e_im - jr3_im;
    wire signed [OB-1:0] c3_re = bp2_re + r3e_re + jr1_re; // y3
    wire signed [OB-1:0] c3_im = bp2_im + r3e_im + jr1_im;
    wire signed [OB-1:0] c7_re = bp2_re - r3e_re - jr1_re; // y7
    wire signed [OB-1:0] c7_im = bp2_im - r3e_im - jr1_im;

    // second level: the y4 chain is PIPELINED (sA_3 registered, then
    // p_1/c4 comb into a late AREG) -- the 3-carry-chain single-clock
    // path was the probe's only timing failure. y0 = round(p0+p1, s2)
    // uses the registered p1 from the PREVIOUS window (the [7G,8G)
    // window's own p1 feeds y4 one hop later; see the golden's y0/y4
    // pairing -- both read the SAME registered p1).
    // p_0 = round(sA_0 + sA_2, s1): sA_2 = the fresh butterfly of the
    // [6G,7G) window; p_1 = round(sA_1 + sA_3, s1) (registered sA_3)
    wire signed [CB-1:0] p0_re_c = rsh_cb(as_r3_re + sA_re_c, SIGMA1);
    wire signed [CB-1:0] p0_im_c = rsh_cb(as_r3_im + sA_im_c, SIGMA1);
    wire signed [CB-1:0] p1_re_c = rsh_cb(as_r3_re + sA_re_c, SIGMA1);
    wire signed [CB-1:0] p1_im_c = rsh_cb(as_r3_im + sA_im_c, SIGMA1);
    wire signed [OB-1:0] p1e_re = {{(OB-CB){p1_r_re[CB-1]}}, p1_r_re};
    wire signed [OB-1:0] p1e_im = {{(OB-CB){p1_r_im[CB-1]}}, p1_r_im};
    wire signed [OB-1:0] c4_re = p0e_re - p1e_re;  // y4 (comb, late AREG)
    wire signed [OB-1:0] c4_im = p0e_im - p1e_im;
    // y0 = round(p0 + p1, s2) (registered into the alignment chain)
    wire signed [CB-1:0] y0_c_re = rsh_cb(bp_r4_re + p1_r_re, SIGMA2);
    wire signed [CB-1:0] y0_c_im = rsh_cb(bp_r4_im + p1_r_im, SIGMA2);

    // the L1 comb outputs are REGISTERED (one fabric stage before the
    // operand mux): the 3-input d-class adds (2 chained CARRY8) would
    // otherwise land on the DSP's absorbed AREG and miss timing. The
    // normal classes and y4 now BOTH capture the AREG at w+4.
    reg signed [OB-1:0] c2r_re, c2r_im, c6r_re, c6r_im;
    reg signed [OB-1:0] c1r_re, c1r_im, c5r_re, c5r_im;
    reg signed [OB-1:0] c3r_re, c3r_im, c7r_re, c7r_im;
    // operand mux (AREG capture): the normal classes decode from k4
    // (the registered combs belong to k4's model slot); the y4 class
    // selects its own combine in the same capture clock, gated by the
    // (k-4) window so the idle/cross-block phases never select it.
    wire w_areg_y4 = ((k - 4) % (8*G)) >= 7*G;
    wire [2:0] cls_op = cls_of(k4);
    wire signed [OB-1:0] m_re_n = (cls_op == 3'd1) ? c2r_re :
                                  (cls_op == 3'd2) ? c6r_re :
                                  (cls_op == 3'd3) ? c1r_re :
                                  (cls_op == 3'd4) ? c5r_re :
                                  (cls_op == 3'd5) ? c3r_re :
                                  (cls_op == 3'd6) ? c7r_re :
                                  {{(OB-1){1'b0}}, 1'b0};
    wire signed [OB-1:0] m_im_n = (cls_op == 3'd1) ? c2r_im :
                                  (cls_op == 3'd2) ? c6r_im :
                                  (cls_op == 3'd3) ? c1r_im :
                                  (cls_op == 3'd4) ? c5r_im :
                                  (cls_op == 3'd5) ? c3r_im :
                                  (cls_op == 3'd6) ? c7r_im :
                                  {{(OB-1){1'b0}}, 1'b0};
    // (the y4 class selects via w_areg_y4 in the capture; m_*_n is the
    // normal-class mux only)

    // ---------------- rotate units ------------------------------------
    // 3-stage pipelined ternary tree: R1 = 4 partials (taps 0-2, 3-5)
    // + the 7th tap wires; R2 = A/B sums; L3 = final add with the round
    // folded in, >>> TD, slice to CB. js selects the output signs:
    //   fwd: re' = A+B, im' = B-A;  inv: re' = A-B, im' = A+B
    // (A = sum of the xr tap terms, B = sum of the js-flipped xi terms)
    // unit A input: the L0 regs hold ringA_d1[(g+3)%G] in the window
    // the 2-stage read shifts the windows: unit A's addr presents at
    // 10G-4+g (the +4 group remap), R1 at 10G-2+g, R2 at 10G-1+g, the
    // ringR write at 10G+g (the model phase).
    wire w_rotA_r1 = ((k + 2) % (8*G) >= 2*G) &&
                     ((k + 2) % (8*G) < 3*G);
    wire w_rotA_r2 = ((k + 1) % (8*G) >= 2*G) &&
                     ((k + 1) % (8*G) < 3*G);
    // unit B input: dA_3; G==1 uses the L1-comb (write at model +3),
    // G>1 the registered dA (write at model +5) -- both before the
    // first d-class slot read at 11G+g (G>1: 5 <= 4G holds for G >= 2).
    wire w_rotB_r1 = (G == 1) ? (k1 >= 7*G) : (k4 >= 7*G);
    wire w_rotB_r2 = (G == 1) ? (k2 >= 7*G) : (k5 >= 7*G);
    wire signed [OW-1:0] round_c = {{(OW-TD-1){1'b0}}, 1'b1} <<< (TD-1);

    // rot tree inputs (js fold: fwd sums xr+xi / xi-xr; inv xr-xi / xi+xr)
    wire signed [OW-1:0] zA_re = {{(OW-CB){ad1_r3_re[CB-1]}}, ad1_r3_re};
    wire signed [OW-1:0] zA_im = {{(OW-CB){ad1_r3_im[CB-1]}}, ad1_r3_im};
    // G==1: unit B runs one clock earlier off the L1 comb (r1 stage)
    wire signed [CB-1:0] dB_re = (G == 1) ? dA_f_re : dA_r3_re;
    wire signed [CB-1:0] dB_im = (G == 1) ? dA_f_im : dA_r3_im;
    wire signed [OW-1:0] zB_re = {{(OW-CB){dB_re[CB-1]}}, dB_re};
    wire signed [OW-1:0] zB_im = {{(OW-CB){dB_im[CB-1]}}, dB_im};

    // R1 partial adds (taps 0-2, 3-5, 6)
    wire signed [OW-1:0] aA1_n = zA_re <<< tap_idx(0);
    wire signed [OW-1:0] aA2_n = zA_re <<< tap_idx(1);
    wire signed [OW-1:0] aA3_n = zA_re <<< tap_idx(2);
    wire signed [OW-1:0] aA4_n = zA_re <<< tap_idx(3);
    wire signed [OW-1:0] aA5_n = zA_re <<< tap_idx(4);
    wire signed [OW-1:0] aA6_n = zA_re <<< tap_idx(5);
    wire signed [OW-1:0] aA7_n = zA_re <<< tap_idx(6);
    wire signed [OW-1:0] aB1_n = zA_im <<< tap_idx(0);
    wire signed [OW-1:0] aB2_n = zA_im <<< tap_idx(1);
    wire signed [OW-1:0] aB3_n = zA_im <<< tap_idx(2);
    wire signed [OW-1:0] aB4_n = zA_im <<< tap_idx(3);
    wire signed [OW-1:0] aB5_n = zA_im <<< tap_idx(4);
    wire signed [OW-1:0] aB6_n = zA_im <<< tap_idx(5);
    wire signed [OW-1:0] aB7_n = zA_im <<< tap_idx(6);
    wire signed [OW-1:0] bA1_n = zB_re <<< tap_idx(0);
    wire signed [OW-1:0] bA2_n = zB_re <<< tap_idx(1);
    wire signed [OW-1:0] bA3_n = zB_re <<< tap_idx(2);
    wire signed [OW-1:0] bA4_n = zB_re <<< tap_idx(3);
    wire signed [OW-1:0] bA5_n = zB_re <<< tap_idx(4);
    wire signed [OW-1:0] bA6_n = zB_re <<< tap_idx(5);
    wire signed [OW-1:0] bA7_n = zB_re <<< tap_idx(6);
    wire signed [OW-1:0] bB1_n = zB_im <<< tap_idx(0);
    wire signed [OW-1:0] bB2_n = zB_im <<< tap_idx(1);
    wire signed [OW-1:0] bB3_n = zB_im <<< tap_idx(2);
    wire signed [OW-1:0] bB4_n = zB_im <<< tap_idx(3);
    wire signed [OW-1:0] bB5_n = zB_im <<< tap_idx(4);
    wire signed [OW-1:0] bB6_n = zB_im <<< tap_idx(5);
    wire signed [OW-1:0] bB7_n = zB_im <<< tap_idx(6);

    // R1 regs + R2 regs + L3 outputs
    reg signed [OW-1:0] aA1, aA2, aB1, aB2, aAlo, aBlo;   // unit A R1
    reg signed [OW-1:0] bA1, bA2, bB1, bB2, bAlo, bBlo;   // unit B R1
    reg signed [OW-1:0] aA, aB;                            // unit A R2
    reg signed [OW-1:0] bA, bB;                            // unit B R2
    // L3: final add (round folded), >>> TD, slice to CB
    wire signed [OW-1:0] rotA_re_full = INVERSE ?
        ((aA - aB) + round_c) : ((aA + aB) + round_c);
    wire signed [OW-1:0] rotA_im_full = INVERSE ?
        ((aA + aB) + round_c) : ((aB - aA) + round_c);
    wire signed [OW-1:0] rotB_re_full = INVERSE ?
        ((bA - bB) + round_c) : ((bA + bB) + round_c);
    wire signed [OW-1:0] rotB_im_full = INVERSE ?
        ((bA + bB) + round_c) : ((bB - bA) + round_c);
    wire signed [CB-1:0] rotA_re = rotA_re_full >>> TD;
    wire signed [CB-1:0] rotA_im = rotA_im_full >>> TD;
    wire signed [CB-1:0] rotB_re = rotB_re_full >>> TD;
    wire signed [CB-1:0] rotB_im = rotB_im_full >>> TD;

    // ---------------- product pipeline regs ---------------------------
    reg signed [OB-1:0] m_r_re, m_r_im;              // AREG
    reg signed [TW-1:0] tr_r, ti_r;                  // BREG
    reg signed [OB-1:0] m_h_re, m_h_im;              // frozen re operand
    reg signed [TW-1:0] tr_h, ti_h;
    reg signed [MWB-1:0] prod_im_re, prod_im_ti;     // L2 (MREG)
    reg signed [MWB-1:0] c_c_re, c_c_im;             // L3 (CREG)
    reg signed [MWB-1:0] prod_re_re, prod_re_ti;     // L3 (MREG)
    reg signed [MWB-1:0] p_re, p_im;                 // L4 (PREG)
    reg signed [PW-1:0] shift_p_re, shift_p_im;      // L5
    reg [2*IW-1:0] y0_pipe [0:7];   // {im, re} pairs (y0 has no DSP)
    // pfifo write precompute: the write address / WE / data are
    // registered one cycle ahead of the write edge so the deep
    // LUTRAM chunk write-enable decodes from registers (the
    // combinational k->WE decode misses 2 ns at 8G depth). The
    // write therefore lands at w+10 instead of w+9; every class's
    // write->read lag grows by 1 (min G-9 >= 7) and the emission
    // schedule is untouched. The pfifo deliberately stays LUTRAM:
    // as BRAM the 2x RAMB36 cascade clk-to-out (~1.4 ns) cannot
    // cross the y0-select mux into the next stage at 2 ns.
    reg [2:0] cls_r;
    reg [GW-1:0] g_r;
    reg w_pf_r;
    reg [IW-1:0] pf_d_re, pf_d_im;

    // per-class fused shift amounts
    localparam integer SH_D = TD + SIGMA0 + SIGMA1 + SIGMA2;  // y1/5/3/7
    localparam integer SH_Q = TD + SIGMA1 + SIGMA2;           // y2/6
    localparam integer SH_P = TD + SIGMA2;                    // y4

    // delayed window gates (k_d = the model phase d clocks ago)
    wire d2_a4 = (k3 >= 4*G) && (k3 < 5*G);
    wire d2_a5 = (k3 >= 5*G) && (k3 < 6*G);
    // the second-level ring writes fire at w+4: the combines consume
    // the registered fresh combs + the 4th read stage (1 adder each);
    // addr/data group = k4's group (the golden's ring index)
    wire w6 = (k4 >= 6*G) && (k4 < 7*G);
    wire w7 = (k4 >= 7*G);

    // pfifo write: every class at w+9 (k9-gated) -- the registered combs
    // moved the normal AREG to w+4, same as y4's
    wire [2:0] cls_w = cls_of(k9);
    wire [GW-1:0] g_w = (G > 1) ? k9[GW-1:0] : {GW{1'b0}};
    wire [GW-1:0] g_w2 = (G > 1) ? k3[GW-1:0] : {GW{1'b0}};
    // the second-level writes: addr = the combine's model group (k4)
    wire [GW-1:0] g_w4 = (G > 1) ? k4[GW-1:0] : {GW{1'b0}};
    wire w_pf = (cls_w != 3'd0) &&
                !(((k - 9) % (8*G)) >= 7*G);
    // the y4 window (k-9 in [7G,8G)) is disjoint from every normal
    // window now, so one write port with a muxed address suffices
    wire w_pf4 = ((k - 9) % (8*G)) >= 7*G;
    wire w_pf_any = w_pf || w_pf4;

    // emission mux: the writes land at w+10 (precomputed addr/WE/data)
    // and the emission read runs at w+10 too (k10-based), one cycle
    // later than the product's original w+9 schedule -- every class's
    // write->read lag is preserved (the y1/y5/y3/y7/y4 classes sit at
    // the minimum lag of exactly 1). The registered emission costs one
    // extra output register (the stage latency grows by 1).
    // t = (k10 - 1) mod 8G; member = (t/G + 1) % 8; member 0 = y0
    wire [KW-1:0] t_e = k10 - {{(KW-1){1'b0}}, 1'b1};
    wire [GW-1:0] g_e = (G > 1) ? t_e[GW-1:0] : {GW{1'b0}};
    wire [2:0] t_div = (G > 1) ? t_e[KW-1:GW] : 3'd0;
    wire [2:0] mm = t_div + 3'd1;                    // (t/G + 1) mod 8
    wire w_y0_out = (t_div == 3'd7);                 // member 0
    // member -> pfifo base: 1->7G(y4) 2->0(y2) 3->G(y6) 4->3G(y1)
    //                        5->4G(y5) 6->5G(y3) 7->6G(y7)
    function [KW-1:0] member_base;
        input [2:0] m_;
        begin
            case (m_)
                3'd1: member_base = 7*G;
                3'd2: member_base = 0;
                3'd3: member_base = G;
                3'd4: member_base = 3*G;
                3'd5: member_base = 4*G;
                3'd6: member_base = 5*G;
                3'd7: member_base = 6*G;
                default: member_base = 0;
            endcase
        end
    endfunction

    // async per-class reads (RAM64X1D-style: the write address g_r and
    // the read address g_e stay independent -- an in-block sync read
    // makes Vivado share one address port and mux g_r into the read
    // path, which misses 2 ns)

    // shift-amount select (the shift comb runs at w+8; k8 = the product's
    // model phase for ALL classes -- the y4 AREG is no later now)
    wire [2:0] cls_sh = cls_of(k8);
    wire signed [PW-1:0] p_re_pw = {{(PW-MWB){p_re[MWB-1]}}, p_re};
    wire signed [PW-1:0] p_im_pw = {{(PW-MWB){p_im[MWB-1]}}, p_im};
    wire w_sh_y4 = ((k - 8) % (8*G)) >= 7*G;
    wire signed [PW-1:0] shift_re_m = w_sh_y4 ?
        rsh_pw(p_re_pw, SH_P) :
        ((cls_sh == 3'd1 || cls_sh == 3'd2) ?
         rsh_pw(p_re_pw, SH_Q) : rsh_pw(p_re_pw, SH_D));
    wire signed [PW-1:0] shift_im_m = w_sh_y4 ?
        rsh_pw(p_im_pw, SH_P) :
        ((cls_sh == 3'd1 || cls_sh == 3'd2) ?
         rsh_pw(p_im_pw, SH_Q) : rsh_pw(p_im_pw, SH_D));

    // rotate ringR write gates/addresses (the +3 hop shifts g by 3;
    // unit B's write offset is +3 for G==1 and +4 for G>1)
    wire w_r1w = ((k - 2*G) % (8*G)) < G;            // unit A: 10G+g
    wire [GW-1:0] g_r1 = g_addr;
    wire w_r3w = (G == 1) ? (((k - 3) % (8*G)) >= 7*G)
                          : (((k - 6) % (8*G)) >= 7*G);
    // the golden's ringR[G+x] = rot(dA_3(group x)) (same pre-shift
    // snapshot cancellation); the RTL's write@t_w stores rot(dA(t_w-6))
    // at (k-6) mod G = the dA's own group
    wire [GW-1:0] g_r3 = (G == 1) ? {GW{1'b0}}
                                  : ((k[GW-1:0] - 3'd6) % G);

    always @(posedge clk) begin
        if (rst) begin
            k <= K_PRELOAD[KW-1:0];
            k1 <= K_PRELOAD[KW-1:0]; k2 <= K_PRELOAD[KW-1:0];
            k3 <= K_PRELOAD[KW-1:0]; k4 <= K_PRELOAD[KW-1:0];
            k5 <= K_PRELOAD[KW-1:0]; k6 <= K_PRELOAD[KW-1:0];
            k7 <= K_PRELOAD[KW-1:0]; k8 <= K_PRELOAD[KW-1:0];
            k9 <= K_PRELOAD[KW-1:0]; k10 <= K_PRELOAD[KW-1:0];
            tw_dout <= 0; tw_h1 <= 0; tw_h2 <= 0; tw_h3 <= 0; tw_h4 <= 0;
            a0_r_re <= 0; a0_r_im <= 0;
            as_r_re <= 0; as_r_im <= 0;
            ad0_r_re <= 0; ad0_r_im <= 0;
            ad1_r_re <= 0; ad1_r_im <= 0;
            bp_r_re <= 0; bp_r_im <= 0;
            bq_r_re <= 0; bq_r_im <= 0;
            bq1_r_re <= 0; bq1_r_im <= 0;
            bm_r_re <= 0; bm_r_im <= 0;
            bp2_r_re <= 0; bp2_r_im <= 0;
            r1_r_re <= 0; r1_r_im <= 0;
            r3_r_re <= 0; r3_r_im <= 0;
            as_r4_re <= 0; as_r4_im <= 0;
            ad0_r4_re <= 0; ad0_r4_im <= 0;
            bp_r4_re <= 0; bp_r4_im <= 0;
            sA_r_re <= 0; sA_r_im <= 0;
            jmdA_r_re <= 0; jmdA_r_im <= 0;
            ringB_p_w_re <= 0; ringB_p_w_im <= 0;
            ringB_q_w_re <= 0; ringB_q_w_im <= 0;
            rbbm_w_re <= 0; rbbm_w_im <= 0;
            rbbp_w_re <= 0; rbbp_w_im <= 0;
            w6_r <= 1'b0; w7_r <= 1'b0; g_w4_r <= 0;
            c2r_re <= 0; c2r_im <= 0; c6r_re <= 0; c6r_im <= 0;
            c1r_re <= 0; c1r_im <= 0; c5r_re <= 0; c5r_im <= 0;
            c3r_re <= 0; c3r_im <= 0; c7r_re <= 0; c7r_im <= 0;
            x_r1_re <= 0; x_r1_im <= 0;
            m_r_re <= 0; m_r_im <= 0; tr_r <= 0; ti_r <= 0;
            m_h_re <= 0; m_h_im <= 0; tr_h <= 0; ti_h <= 0;
            prod_im_re <= 0; prod_im_ti <= 0;
            c_c_re <= 0; c_c_im <= 0;
            prod_re_re <= 0; prod_re_ti <= 0;
            p_re <= 0; p_im <= 0;
            shift_p_re <= 0; shift_p_im <= 0;
            aA1 <= 0; aA2 <= 0; aB1 <= 0; aB2 <= 0; aAlo <= 0; aBlo <= 0;
            bA1 <= 0; bA2 <= 0; bB1 <= 0; bB2 <= 0; bAlo <= 0; bBlo <= 0;
            aA <= 0; aB <= 0; bA <= 0; bB <= 0;
            dA_r3_re <= 0; dA_r3_im <= 0;
            dA_f_re <= 0; dA_f_im <= 0;
            p1_r_re <= 0; p1_r_im <= 0;
            tw_h1 <= 0; tw_h2 <= 0; tw_h3 <= 0; tw_h4 <= 0;
            y0_pipe[0] <= 0; y0_pipe[1] <= 0; y0_pipe[2] <= 0;
            y0_pipe[3] <= 0; y0_pipe[4] <= 0; y0_pipe[5] <= 0;
            y0_pipe[6] <= 0; y0_pipe[7] <= 0;
            cls_r <= 0; g_r <= 0; w_pf_r <= 1'b0;
            pf_d_re <= 0; pf_d_im <= 0;
        end else if (ce) begin
            // ---- L0: capture reads + input + twiddle DOUT ----
            tw_dout <= tw_rom[rom_addr];
            a0_r_re <= ring0_re[a0_addr];
            a0_r_im <= ring0_im[a0_addr];
            as_r_re <= ringA_s_re[as_addr]; as_r_im <= ringA_s_im[as_addr];
            ad0_r_re <= ringA_d0_re[g_addr]; ad0_r_im <= ringA_d0_im[g_addr];
            ad1_r_re <= ringA_d1_re[g_a3]; ad1_r_im <= ringA_d1_im[g_a3];
            bp_r_re <= ringB_p_re[g_addr]; bp_r_im <= ringB_p_im[g_addr];
            bq_r_re <= ringB_q_re[g_addr]; bq_r_im <= ringB_q_im[g_addr];
            bq1_r_re <= ringB_q1_re[g_addr]; bq1_r_im <= ringB_q1_im[g_addr];
            bm_r_re <= rbbm_re[g_addr]; bm_r_im <= rbbm_im[g_addr];
            bp2_r_re <= rbbp_re[g_addr]; bp2_r_im <= rbbp_im[g_addr];
            r1_r_re <= rr1_re[g_addr]; r1_r_im <= rr1_im[g_addr];
            r3_r_re <= rr3_re[g_addr]; r3_r_im <= rr3_im[g_addr];
            x_r1_re <= x_re; x_r1_im <= x_im;

            // ---- r2: the fabric stage after the RAM output regs ----
            a0_r2_re <= a0_r_re;  a0_r2_im <= a0_r_im;
            as_r2_re <= as_r_re;  as_r2_im <= as_r_im;
            ad0_r2_re <= ad0_r_re; ad0_r2_im <= ad0_r_im;
            ad1_r2_re <= ad1_r_re; ad1_r2_im <= ad1_r_im;
            bp_r2_re <= bp_r_re;  bp_r2_im <= bp_r_im;
            bq_r2_re <= bq_r_re;  bq_r2_im <= bq_r_im;
            bq1_r2_re <= bq1_r_re; bq1_r2_im <= bq1_r_im;
            bm_r2_re <= bm_r_re;  bm_r2_im <= bm_r_im;
            bp2_r2_re <= bp2_r_re; bp2_r2_im <= bp2_r_im;
            r1_r2_re <= r1_r_re;  r1_r2_im <= r1_r_im;
            r3_r2_re <= r3_r_re;  r3_r2_im <= r3_r_im;
            x_r2_re <= x_r1_re;   x_r2_im <= x_r1_im;
            a0_r3_re <= a0_r2_re;  a0_r3_im <= a0_r2_im;
            as_r3_re <= as_r2_re;  as_r3_im <= as_r2_im;
            ad0_r3_re <= ad0_r2_re; ad0_r3_im <= ad0_r2_im;
            ad1_r3_re <= ad1_r2_re; ad1_r3_im <= ad1_r2_im;
            bp_r3_re <= bp_r2_re;  bp_r3_im <= bp_r2_im;
            bq_r3_re <= bq_r2_re;  bq_r3_im <= bq_r2_im;
            bq1_r3_re <= bq1_r2_re; bq1_r3_im <= bq1_r2_im;
            bm_r3_re <= bm_r2_re;  bm_r3_im <= bm_r2_im;
            bp2_r3_re <= bp2_r2_re; bp2_r3_im <= bp2_r2_im;
            r1_r3_re <= r1_r2_re;  r1_r3_im <= r1_r2_im;
            r3_r3_re <= r3_r2_re;  r3_r3_im <= r3_r2_im;
            x_r3_re <= x_r2_re;   x_r3_im <= x_r2_im;
            dA_f_re <= a0_r2_re - x_r2_re;
            dA_f_im <= a0_r2_im - x_r2_im;
            // r4: the 4th stage (the w+4 second-level combines)
            as_r4_re <= as_r3_re;  as_r4_im <= as_r3_im;
            ad0_r4_re <= ad0_r3_re; ad0_r4_im <= ad0_r3_im;
            bp_r4_re <= bp_r3_re;  bp_r4_im <= bp_r3_im;
            // the fresh-comb delay regs (consumed by the w6/w7 writes)
            sA_r_re <= sA_re_c; sA_r_im <= sA_im_c;
            jmdA_r_re <= INVERSE ? -dA_f_im : dA_f_im;
            jmdA_r_im <= INVERSE ? dA_f_re : -dA_f_re;
            ringB_p_w_re <= rsh_cb(as_r4_re + sA_r_re, SIGMA1);
            ringB_p_w_im <= rsh_cb(as_r4_im + sA_r_im, SIGMA1);
            ringB_q_w_re <= as_r4_re - sA_r_re;
            ringB_q_w_im <= as_r4_im - sA_r_im;
            rbbm_w_re <= ad0_r4_re + jmdA_r_re;
            rbbm_w_im <= ad0_r4_im + jmdA_r_im;
            rbbp_w_re <= ad0_r4_re - jmdA_r_re;
            rbbp_w_im <= ad0_r4_im - jmdA_r_im;
            w6_r <= w6; w7_r <= w7; g_w4_r <= g_w4;
            // the L1 comb output registers (the operand-mux sources)
            c2r_re <= c2_re; c2r_im <= c2_im;
            c6r_re <= c6_re; c6r_im <= c6_im;
            c1r_re <= c1_re; c1r_im <= c1_im;
            c5r_re <= c5_re; c5r_im <= c5_im;
            c3r_re <= c3_re; c3r_im <= c3_im;
            c7r_re <= c7_re; c7r_im <= c7_im;
            // the registered dA (rot unit B input, G>1): gated at k3 so
            // dA_r3@t = the dA of phase t-4 -- the rot-B comb@p+4 then
            // sees the dA_3 of group p and the write@p+6 lands at index p
            if ((k3 % (8*G)) >= 7*G) begin
                dA_r3_re <= dA_f_re; dA_r3_im <= dA_f_im;
            end
            // p_1 registered at w+4 (the y4/y0 combs consume it with
            // the registered ringB_p read at w+4)
            if ((k3 % (8*G)) >= 7*G) begin
                p1_r_re <= p1_re_c; p1_r_im <= p1_im_c;
            end

            // ---- AREG/BREG capture (operands from the L1 comb;
            // the y4 class captures one clock later with its own
            // operand/twiddle hold stages) ----
            if (w_areg_y4) begin
                m_r_re <= c4_re; m_r_im <= c4_im;
                tr_r <= tr4_w; ti_r <= ti4_w;
            end else begin
                m_r_re <= m_re_n; m_r_im <= m_im_n;
                tr_r <= tr_w; ti_r <= ti_w;
            end

            // ---- L2: im products + freeze ----
            prod_im_re <= m_r_im * tr_r;   // bc (the p_im cross term)
            prod_im_ti <= m_r_im * ti_r;   // bd
            m_h_re <= m_r_re; m_h_im <= m_r_im;
            tr_h <= tr_r; ti_h <= ti_r;
            tw_h1 <= tw_dout; tw_h2 <= tw_h1; tw_h3 <= tw_h2;
            tw_h4 <= tw_h3;

            // ---- L3: re products + C-port regs ----
            prod_re_re <= m_h_re * tr_h;
            prod_re_ti <= m_h_re * ti_h;
            c_c_re <= prod_im_re; c_c_im <= prod_im_ti;

            // ---- L4: ALU combine (PREG) ----
            p_re <= prod_re_re - c_c_im;
            p_im <= prod_re_ti + c_c_re;

            // ---- L5: per-class fused shift staging ----
            shift_p_re <= shift_re_m;
            shift_p_im <= shift_im_m;

            // ---- L6: pfifo write (at w+10, precomputed) + emission ----
            cls_r    <= w_pf4 ? 3'd7 : cls_w;
            g_r      <= g_w;
            w_pf_r   <= w_pf_any;
            pf_d_re  <= shift_p_re[IW-1:0];
            pf_d_im  <= shift_p_im[IW-1:0];
            if (w_pf_r) begin
                case (cls_r)
                    3'd1: begin pf1_re[g_r] <= pf_d_re; pf1_im[g_r] <= pf_d_im; end
                    3'd2: begin pf2_re[g_r] <= pf_d_re; pf2_im[g_r] <= pf_d_im; end
                    3'd3: begin pf3_re[g_r] <= pf_d_re; pf3_im[g_r] <= pf_d_im; end
                    3'd4: begin pf4_re[g_r] <= pf_d_re; pf4_im[g_r] <= pf_d_im; end
                    3'd5: begin pf5_re[g_r] <= pf_d_re; pf5_im[g_r] <= pf_d_im; end
                    3'd6: begin pf6_re[g_r] <= pf_d_re; pf6_im[g_r] <= pf_d_im; end
                    default: begin pf7_re[g_r] <= pf_d_re; pf7_im[g_r] <= pf_d_im; end
                endcase
            end
            // ---- butterfly ring writes (data = L1 comb, gate k1) ----
            if (d2_a4) begin
                ringA_s_re[as_addr_w] <= sA_re_c;
                ringA_s_im[as_addr_w] <= sA_im_c;
                ringA_d0_re[g_w2] <= dA_f_re;
                ringA_d0_im[g_w2] <= dA_f_im;
            end
            if (d2_a5) begin
                ringA_s_re[as_addr_w] <= sA_re_c;
                ringA_s_im[as_addr_w] <= sA_im_c;
                ringA_d1_re[g_w2] <= dA_f_re;
                ringA_d1_im[g_w2] <= dA_f_im;
            end
            if (w6_r) begin
                // p_0 = round(sA_0+sA, s1); q_0 = sA_0 - sA
                // bm = d0 + js*j*d2, bp = d0 - js*j*d2
                // (the data/gate/address registered one cycle ahead)
                ringB_p_re[g_w4_r] <= ringB_p_w_re;
                ringB_p_im[g_w4_r] <= ringB_p_w_im;
                ringB_q_re[g_w4_r] <= ringB_q_w_re;
                ringB_q_im[g_w4_r] <= ringB_q_w_im;
                rbbm_re[g_w4_r] <= rbbm_w_re;
                rbbm_im[g_w4_r] <= rbbm_w_im;
                rbbp_re[g_w4_r] <= rbbp_w_re;
                rbbp_im[g_w4_r] <= rbbp_w_im;
            end
            if (w7_r) begin
                // q_1 = sA_1 - sA
                ringB_q1_re[g_w4_r] <= ringB_q_w_re;
                ringB_q1_im[g_w4_r] <= ringB_q_w_im;
            end

            // ---- ring0 write (raw input, gate k) ----
            if (k < 4*G) begin
                ring0_re[a0_addr] <= in_re;
                ring0_im[a0_addr] <= in_im;
            end

            // ---- rotate R1 enables (partials from the L0/L1 regs) --
            if (w_rotA_r1) begin
                aA1 <= aA1_n + aA2_n + aA3_n;   // taps 0-2
                aA2 <= aA4_n + aA5_n + aA6_n;   // taps 3-5
                aAlo <= aA7_n;                  // tap 6
                aB1 <= aB1_n + aB2_n + aB3_n;
                aB2 <= aB4_n + aB5_n + aB6_n;
                aBlo <= aB7_n;
            end
            if (w_rotB_r1) begin
                bA1 <= bA1_n + bA2_n + bA3_n;
                bA2 <= bA4_n + bA5_n + bA6_n;
                bAlo <= bA7_n;
                bB1 <= bB1_n + bB2_n + bB3_n;
                bB2 <= bB4_n + bB5_n + bB6_n;
                bBlo <= bB7_n;
            end
            // rotate R2 sums (gated; hold between windows)
            if (w_rotA_r2) begin
                aA <= aA1 + aA2 + aAlo;
                aB <= aB1 + aB2 + aBlo;
            end
            if (w_rotB_r2) begin
                bA <= bA1 + bA2 + bAlo;
                bB <= bB1 + bB2 + bBlo;
            end

            // ---- y0 alignment chain ----
            y0_pipe[0] <= {y0_c_im[IW-1:0], y0_c_re[IW-1:0]};
            y0_pipe[1] <= y0_pipe[0];
            y0_pipe[2] <= y0_pipe[1];
            y0_pipe[3] <= y0_pipe[2];
            y0_pipe[4] <= y0_pipe[3];
            y0_pipe[5] <= y0_pipe[4];
            y0_pipe[6] <= y0_pipe[5];
            y0_pipe[7] <= y0_pipe[6];

            // ---- phase ----
            k <= k_next;
            k1 <= k; k2 <= k1; k3 <= k2; k4 <= k3; k5 <= k4; k6 <= k5;
            k7 <= k6; k8 <= k7; k9 <= k8; k10 <= k9;
        end
    end

    // ---- emission: per-class read registers + the member mux ----
    // Any RAM deeper than 32 needs an output FD stage: qN registers the
    // LUTRAM read (the write address g_r and the read address g_e then
    // live on separate primitive ports with only short local routes),
    // and the member mux runs register-to-register. The emission lands
    // one cycle later than the async-read version (mm_r is mm delayed
    // to stay paired with the qN data; y0 uses the next pipe tap).
    reg [IW-1:0] em1_re, em1_im, em2_re, em2_im, em3_re, em3_im, em4_re, em4_im;
    reg [IW-1:0] em5_re, em5_im, em6_re, em6_im, em7_re, em7_im;
    reg [2:0] mm_r;
    always @(posedge clk) begin
        if (rst) begin
            em1_re <= 0; em1_im <= 0; em2_re <= 0; em2_im <= 0;
            em3_re <= 0; em3_im <= 0; em4_re <= 0; em4_im <= 0;
            em5_re <= 0; em5_im <= 0; em6_re <= 0; em6_im <= 0;
            em7_re <= 0; em7_im <= 0;
            mm_r <= 0; out_re <= 0; out_im <= 0;
        end else if (ce) begin
            em1_re <= pf1_re[g_e]; em1_im <= pf1_im[g_e];
            em2_re <= pf2_re[g_e]; em2_im <= pf2_im[g_e];
            em3_re <= pf3_re[g_e]; em3_im <= pf3_im[g_e];
            em4_re <= pf4_re[g_e]; em4_im <= pf4_im[g_e];
            em5_re <= pf5_re[g_e]; em5_im <= pf5_im[g_e];
            em6_re <= pf6_re[g_e]; em6_im <= pf6_im[g_e];
            em7_re <= pf7_re[g_e]; em7_im <= pf7_im[g_e];
            mm_r <= mm;
            case (mm_r)
                3'd1: begin out_re <= em7_re; out_im <= em7_im; end
                3'd2: begin out_re <= em1_re; out_im <= em1_im; end
                3'd3: begin out_re <= em2_re; out_im <= em2_im; end
                3'd4: begin out_re <= em3_re; out_im <= em3_im; end
                3'd5: begin out_re <= em4_re; out_im <= em4_im; end
                3'd6: begin out_re <= em5_re; out_im <= em5_im; end
                3'd7: begin out_re <= em6_re; out_im <= em6_im; end
                default: begin
                    out_re <= y0_pipe[7][IW-1:0];
                    out_im <= y0_pipe[7][2*IW-1:IW];
                end
            endcase
        end
    end

    // ---- rotate L3 + ringR writes (combinational final adds) --------
    always @(posedge clk) begin
        if (ce) begin
            if (w_r1w) begin
                rr1_re[g_r1] <= rotA_re;
                rr1_im[g_r1] <= rotA_im;
            end
            if (w_r3w) begin
                rr3_re[g_r3] <= rotB_re;
                rr3_im[g_r3] <= rotB_im;
            end
        end
    end

endmodule

`default_nettype wire
