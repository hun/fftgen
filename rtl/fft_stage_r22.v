// fftgen -- one radix-2^2 DIF stage (P7), PIPELINED for timing closure,
// mirroring golden._R22DIFStage register-for-register plus the
// spikes/S5_r22/piped_model.py L0-L5 pipeline (verified bit-exact).
//
// The stage merges the DIF stage pair (2m, 2m+1) into one 4-sample
// group with ONE shared complex multiplier (3 products per group
// instead of 4). Group depth D = N/4^{m+1}, twiddle stride 4^m
// (pre-sliced ROM: [T[g*4^m]], [T[2g*4^m]], [T[3g*4^m]]).
//
// Pipeline (each stage = one register; the phase chain k1..k4 delays
// the capture phase for the per-depth write gates):
//   L1           butterfly (s_x, d_x), sd = s0 - s1, the +/-j
//                combines c1/c3, y0_raw = s0 + s1  (COMBINATIONAL
//                from the async memory reads + the input; captured at
//                the posedge, so the regs hold the CURRENT clock's L1
//                exactly as the model's step does)
//   L2           product operand mux (gate k1) + the 4 multiplies
//                (DSP48E2 AREG/BREG/MREG absorbed)
//   L3           re/im combine (the DSP ALU/PREG)
//   L4           round-half-up staging (products by the k3-selected
//                shift; y0 from the 3-deep y0_raw chain)
//   L5           writes + output mux (per-depth gates)
//
// Writes and depths (from the verified piped model):
//   ram    depth 0 (raw input)        gate k
//   sram/  depth 0 (the CURRENT       gate k (the arrival clock; a
//   dram   combinational s_x/d_x)     nonblocking write cannot be seen
//   dline                              in the same clock, so the lag-D
//                                      lines are written at the arrival)
//   pfifo  depth 4 (shift_p)          gate k4
//   output mux  depth 4 (y0_r)        gate k4; pfifo lag D
//
// Position p's value emerges at clock p + 3D + 5 (latency 3D+5).

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
    // K_PRELOAD = (-upstream_latency) mod 4D); the delayed chain k1..k4
    // is the capture phase at depth 1..4 (reverse-order shifts)
    reg [KW-1:0] k;
    reg [KW-1:0] k1;
    reg [KW-1:0] k2;
    reg [KW-1:0] k3;
    reg [KW-1:0] k4;
    // g = k mod DEPTH (the low clog2(D) bits -- but for D=1 that is 0
    // bits, i.e. g is always 0; a 1-bit slice would give k mod 2)
    localparam integer GBITS = (DEPTH > 1) ? $clog2(DEPTH) : 0;
    wire [DW-1:0] g = (GBITS > 0) ? k[GBITS-1:0] : {DW{1'b0}};
    // phase thresholds at KW width: a narrower declaration would truncate
    // (D=1: 2*DEPTH in 1 bit becomes 0 and phase_a2 is always true)
    localparam [KW-1:0] ONE_D   = DEPTH;
    localparam [KW-1:0] TWO_D   = 2 * DEPTH;
    localparam [KW-1:0] THREE_D = 3 * DEPTH;

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
    wire phase_y1  = (k <  ONE_D);
    wire phase_y3  = (k >= ONE_D && k < TWO_D);
    wire phase_a2  = (k >= TWO_D && k < THREE_D);
    wire phase_cmb = (k >= THREE_D);
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

    // ------------------------------------------------------------------
    // combinational reads (async; read-old at the current pointer)
    // ------------------------------------------------------------------
    wire signed [BW-1:0] a0_re = {{(BW-WIDTH){ram_re[rp][WIDTH-1]}}, ram_re[rp]};
    wire signed [BW-1:0] a0_im = {{(BW-WIDTH){ram_im[rp][WIDTH-1]}}, ram_im[rp]};
    wire signed [BW-1:0] s0_re = {{(BW-WIDTH){sram_re[sp][WIDTH-1]}}, sram_re[sp]};
    wire signed [BW-1:0] s0_im = {{(BW-WIDTH){sram_im[sp][WIDTH-1]}}, sram_im[sp]};
    wire signed [BW-1:0] d0_re = {{(BW-WIDTH){dram_re[sp][WIDTH-1]}}, dram_re[sp]};
    wire signed [BW-1:0] d0_im = {{(BW-WIDTH){dram_im[sp][WIDTH-1]}}, dram_im[sp]};
    wire signed [BW-1:0] d1_re = {{(BW-WIDTH){dline_re[sp][WIDTH-1]}}, dline_re[sp]};
    wire signed [BW-1:0] d1_im = {{(BW-WIDTH){dline_im[sp][WIDTH-1]}}, dline_im[sp]};

    // ------------------------------------------------------------------
    // L1 (COMBINATIONAL from the current reads/input; captured at the
    // posedge so the regs hold the CURRENT clock's L1 like the model)
    // ------------------------------------------------------------------
    wire signed [BW-1:0] s_x_re = round_shift_bw(a0_re + x_re, SIGMA0);
    wire signed [BW-1:0] s_x_im = round_shift_bw(a0_im + x_im, SIGMA0);
    wire signed [BW-1:0] d_x_re = a0_re - x_re;
    wire signed [BW-1:0] d_x_im = a0_im - x_im;
    wire signed [BW-1:0] sd_re = s0_re - s_x_re;   // s0 - s1 (y2)
    wire signed [BW-1:0] sd_im = s0_im - s_x_im;
    // combined diffs (CB): y1 = d0 -/+ j*d1, y3 = d0 +/- j*d1
    wire signed [CB-1:0] se_d0_re = {{(CB-BW){d0_re[BW-1]}}, d0_re};
    wire signed [CB-1:0] se_d0_im = {{(CB-BW){d0_im[BW-1]}}, d0_im};
    wire signed [CB-1:0] se_d1_re = {{(CB-BW){d1_re[BW-1]}}, d1_re};
    wire signed [CB-1:0] se_d1_im = {{(CB-BW){d1_im[BW-1]}}, d1_im};
    wire signed [CB-1:0] js_d1_im = INVERSE ? -se_d1_im : se_d1_im;
    wire signed [CB-1:0] js_d1_re = INVERSE ? -se_d1_re : se_d1_re;
    wire signed [CB-1:0] c1_re = se_d0_re + js_d1_im;  // d0 + js*d1_im
    wire signed [CB-1:0] c1_im = se_d0_im - js_d1_re;  // d0 - js*d1_re
    wire signed [CB-1:0] c3_re = se_d0_re - js_d1_im;
    wire signed [CB-1:0] c3_im = se_d0_im + js_d1_re;
    // y0 = round(s0 + s1, SIGMA1); the raw sum is padded to the product
    // depth (3-deep chain) so the output mux aligns with the pfifo
    wire signed [BW-1:0] y0_raw_re = s0_re + s_x_re;
    wire signed [BW-1:0] y0_raw_im = s0_im + s_x_im;

    // L1 registers (captured at the posedge from the CURRENT values)
    reg [BW-1:0] sd_r_re, sd_r_im;
    reg [CB-1:0] c1_r_re, c1_r_im, c3_r_re, c3_r_im;
    reg [TWIDDLE_WIDTH-1:0] tr_r, ti_r;
    reg [BW-1:0] y0_raw_r_re, y0_raw_r_im;
    reg [BW-1:0] y0_raw2_re, y0_raw2_im;
    reg [BW-1:0] y0_raw3_re, y0_raw3_im;

    // ------------------------------------------------------------------
    // L2: product operand mux (by the k1 phase) + the multiplies
    // ------------------------------------------------------------------
    wire [CB-1:0] m_re = (k1 >= THREE_D) ? {{(CB-BW){sd_r_re[BW-1]}}, sd_r_re}
                        : (k1 < ONE_D ? c1_r_re : c3_r_re);
    wire [CB-1:0] m_im = (k1 >= THREE_D) ? {{(CB-BW){sd_r_im[BW-1]}}, sd_r_im}
                        : (k1 < ONE_D ? c1_r_im : c3_r_im);
    wire signed [MWB-1:0] m_re_w = {{(MWB-CB){m_re[CB-1]}}, m_re};
    wire signed [MWB-1:0] m_im_w = {{(MWB-CB){m_im[CB-1]}}, m_im};
    wire signed [MWB-1:0] tr_w = {{(MWB-TWIDDLE_WIDTH){tr_r[TWIDDLE_WIDTH-1]}}, tr_r};
    wire signed [MWB-1:0] ti_w = {{(MWB-TWIDDLE_WIDTH){ti_r[TWIDDLE_WIDTH-1]}}, ti_r};
    reg [MWB-1:0] prod_rr, prod_ri, prod_ir, prod_ii;   // the DSP MREG

    // ------------------------------------------------------------------
    // L3: re/im combine (the DSP ALU/PREG)
    // ------------------------------------------------------------------
    reg [PW-1:0] p_re, p_im;

    // ------------------------------------------------------------------
    // L4: round-half-up staging (the k3-selected shift; the delayed y0)
    // ------------------------------------------------------------------
    reg [PW-1:0] shift_p_re, shift_p_im;
    reg [WIDTH-1:0] y0_r_re, y0_r_im;

    // L5 gates
    wire w_gate_ram = (k < TWO_D);
    wire w_gate_sd  = (k >= TWO_D && k < THREE_D);
    wire w_gate_dl  = (k >= THREE_D);
    wire w_gate_pf  = (k4 < TWO_D || k4 >= THREE_D);
    wire out_is_y0  = (k4 >= THREE_D);

    always @(posedge clk) begin
        if (rst) begin
            k      <= K_PRELOAD[KW-1:0];
            k1     <= K_PRELOAD[KW-1:0];
            k2     <= K_PRELOAD[KW-1:0];
            k3     <= K_PRELOAD[KW-1:0];
            k4     <= K_PRELOAD[KW-1:0];
            rp     <= {AW{1'b0}};
            pwp    <= {AW{1'b0}};
            pr_r   <= {AW{1'b0}} - DEPTH[AW-1:0];
            sp     <= {DW{1'b0}};
            out_re <= {WIDTH{1'b0}};
            out_im <= {WIDTH{1'b0}};
            sd_r_re <= {BW{1'b0}}; sd_r_im <= {BW{1'b0}};
            c1_r_re <= {CB{1'b0}}; c1_r_im <= {CB{1'b0}};
            c3_r_re <= {CB{1'b0}}; c3_r_im <= {CB{1'b0}};
            tr_r <= {TWIDDLE_WIDTH{1'b0}}; ti_r <= {TWIDDLE_WIDTH{1'b0}};
            y0_raw_r_re <= {BW{1'b0}}; y0_raw_r_im <= {BW{1'b0}};
            y0_raw2_re <= {BW{1'b0}}; y0_raw2_im <= {BW{1'b0}};
            y0_raw3_re <= {BW{1'b0}}; y0_raw3_im <= {BW{1'b0}};
            prod_rr <= {MWB{1'b0}}; prod_ri <= {MWB{1'b0}};
            prod_ir <= {MWB{1'b0}}; prod_ii <= {MWB{1'b0}};
            p_re <= {PW{1'b0}}; p_im <= {PW{1'b0}};
            shift_p_re <= {PW{1'b0}}; shift_p_im <= {PW{1'b0}};
            y0_r_re <= {WIDTH{1'b0}}; y0_r_im <= {WIDTH{1'b0}};
        end else if (ce) begin
            // ---- L1 registers (the CURRENT clock's L1) ----
            sd_r_re <= sd_re;    sd_r_im <= sd_im;
            c1_r_re <= c1_re;    c1_r_im <= c1_im;
            c3_r_re <= c3_re;    c3_r_im <= c3_im;
            tr_r <= tr;          ti_r <= ti;
            y0_raw_r_re <= y0_raw_re;   y0_raw_r_im <= y0_raw_im;
            y0_raw2_re <= y0_raw_r_re;  y0_raw2_im <= y0_raw_r_im;
            y0_raw3_re <= y0_raw2_re;   y0_raw3_im <= y0_raw2_im;
            k1 <= k; k2 <= k1; k3 <= k2; k4 <= k3;

            // ---- L2: the products (MREG) ----
            prod_rr <= m_re_w * tr_w;
            prod_ri <= m_re_w * ti_w;
            prod_ir <= m_im_w * tr_w;
            prod_ii <= m_im_w * ti_w;

            // ---- L3: the re/im combine (ALU) ----
            p_re <= {{(PW-MWB){prod_rr[MWB-1]}}, prod_rr}
                  - {{(PW-MWB){prod_ii[MWB-1]}}, prod_ii};
            p_im <= {{(PW-MWB){prod_ri[MWB-1]}}, prod_ri}
                  + {{(PW-MWB){prod_ir[MWB-1]}}, prod_ir};

            // ---- L4: the round-half-up staging ----
            if (k3 >= THREE_D) begin
                shift_p_re <= round_shift_pw(p_re, TD_PLUS_S1);
                shift_p_im <= round_shift_pw(p_im, TD_PLUS_S1);
            end else begin
                shift_p_re <= round_shift_pw(p_re, TD_PLUS_BOTH);
                shift_p_im <= round_shift_pw(p_im, TD_PLUS_BOTH);
            end
            y0_r_re <= round_shift_bw(y0_raw3_re, SIGMA1)[WIDTH-1:0];
            y0_r_im <= round_shift_bw(y0_raw3_im, SIGMA1)[WIDTH-1:0];

            // ---- L5: the writes and the output ----
            if (w_gate_ram) begin
                ram_re[rp] <= in_re;
                ram_im[rp] <= in_im;
            end
            if (w_gate_sd) begin
                sram_re[sp] <= s_x_re[WIDTH-1:0];
                sram_im[sp] <= s_x_im[WIDTH-1:0];
                dram_re[sp] <= d_x_re[WIDTH-1:0];
                dram_im[sp] <= d_x_im[WIDTH-1:0];
            end
            if (w_gate_dl) begin
                dline_re[sp] <= d_x_re[WIDTH-1:0];
                dline_im[sp] <= d_x_im[WIDTH-1:0];
            end
            if (w_gate_pf) begin
                pfifo_re[pwp] <= shift_p_re[WIDTH-1:0];
                pfifo_im[pwp] <= shift_p_im[WIDTH-1:0];
            end
            out_re <= out_is_y0 ? y0_r_re : pfifo_re[pr_r];
            out_im <= out_is_y0 ? y0_r_im : pfifo_im[pr_r];

            // ---- pointers ----
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
