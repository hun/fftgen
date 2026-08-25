// fftgen -- SSR cross-lane combine (PLAN.md 2.4).
//
// Inputs: R lanes of native-order sub-FFT outputs A_r[p], one p per clock
// (all lanes lockstep-aligned; in_valid asserts only when lane data is
// valid). Applies the pre-twiddle W_N^{r*p} and the R-point DFT across
// lanes; emits the R-wide output word
//     out[q] = X[q*M + p]
// so over a frame output lane q carries the contiguous block
// X[qM .. qM+M-1].
//
// Fixed-point contract (mirrors golden_ssr.SSRGoldenModel.tick):
//   * EVERY lane (including r = 0) is scaled by its Q(td) pre-twiddle
//     row entry -- skipping row 0 would make one lane's contribution
//     2^td times smaller and the butterfly sums would cancel it
//   * R <= 4: the lane-DFT coefficients are exactly {0,+/-1,+/-j}, so
//     the combine network is add/sub/swap only (no multipliers)
//   * accumulation full precision; one fused rounding shift s_x=log2(R)
//   * round-half-up rescale Q(od + td) -> Q(od), saturate to ow
//
// Emission is frame-synced: words are dropped until the first p == 0
// slot at or after the pipeline has filled (CB_LAT stages), then every
// word emits.
//
// v1 implements R in {2, 4}: the R-point DFT unrolls as log2(R) radix-2
// layers over the pre-twiddled lanes (W_R coefficients for R <= 4 need
// no multipliers). R >= 8 requires constant-multiplier layers at
// generation time (W_8 parts involve sqrt(2)/2).
`default_nettype none

module fft_cross #(
    parameter integer NUM_POINTS = 8,        // full N
    parameter integer SSR        = 2,        // R
    parameter integer OUT_WIDTH  = 16,
    parameter integer OUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH = 18,
    parameter integer TWIDDLE_DECIMAL = 17,
    parameter         WN_FILE    = "fft_wn.mem"   // R*M words, row r = W_N^{r*p}
)(
    input  wire                        clk,
    input  wire                        ce,
    input  wire                        rst,
    input  wire                        in_valid,
    // packed lane words: lane i at [i*OUT_WIDTH +: OUT_WIDTH]
    input  wire signed [SSR*OUT_WIDTH-1:0] din_re,
    input  wire signed [SSR*OUT_WIDTH-1:0] din_im,

    output wire                        out_valid,
    output reg  signed [SSR*OUT_WIDTH-1:0] dout_re,
    output reg  signed [SSR*OUT_WIDTH-1:0] dout_im
);

    localparam integer N  = NUM_POINTS;
    localparam integer R  = SSR;
    localparam integer M  = N / R;
    localparam integer MW = (M > 1) ? $clog2(M) : 1;
    localparam integer PW = OUT_WIDTH + TWIDDLE_WIDTH;
    // accumulate: products (PW bits) summed over R terms, +2 guard
    localparam integer AW = PW + $clog2(R) + 2;
    localparam integer SX = $clog2(R);
    localparam integer RESHIFT = TWIDDLE_DECIMAL;
    // pipeline: stage1 pre-twiddle -> stage2 DFT layer -> stage3 rescale
    localparam integer CB_LAT = 4;

    // pre-twiddle ROM: R rows x M columns; row r holds W_N^{r*p}
    // INCLUDING r = 0 (W = 1.0 in Q(td)) -- every lane must be scaled
    // identically or the butterfly sums cancel one lane entirely
    (* ram_style = "distributed" *)
    reg signed [TWIDDLE_WIDTH*2-1:0] wn_rom [0:R*M-1];
    initial begin
        $readmemh(WN_FILE, wn_rom);
    end

    // per-lane pre-twiddle coefficient (row r at [r*M + p])
    wire signed [TWIDDLE_WIDTH-1:0] w_re [0:R-1];
    wire signed [TWIDDLE_WIDTH-1:0] w_im [0:R-1];
    genvar gr;
    generate
        for (gr = 0; gr < R; gr = gr + 1) begin : wn_row
            assign w_re[gr] = wn_rom[gr*M + p][TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
            assign w_im[gr] = wn_rom[gr*M + p][TWIDDLE_WIDTH-1:0];
        end
    endgenerate

    // slot phase within a frame; pd..pd3 delay it through the three
    // pipeline stages so pd3 == 0 marks an output word that is a frame
    // start (the content word had phase p == 0)
    reg [MW-1:0] p, pd, pd2, pd3;
    reg [MW+3:0] scnt;
    reg          synced;
    wire         run = ce && in_valid;
    wire         mature = $unsigned(scnt) > (CB_LAT + 1);
    wire         out_phase0 = mature && (pd2 == {{(MW-1){1'b0}}, 1'b0});

    localparam integer OW = OUT_WIDTH;

    // ---- stage 1a: coefficient prefetch (registered ROM output) ------
    reg signed [TWIDDLE_WIDTH-1:0] wq_re [0:R-1];
    reg signed [TWIDDLE_WIDTH-1:0] wq_im [0:R-1];
    // ---- stage 1b operands: din delayed one cycle --------------------
    reg signed [OUT_WIDTH-1:0] d_re [0:R-1];
    reg signed [OUT_WIDTH-1:0] d_im [0:R-1];
    // ---- stage 1b products (full precision registers) ----------------
    reg signed [PW-1:0] b_re [0:R-1];
    reg signed [PW-1:0] b_im [0:R-1];
    reg                 v1;
    wire [MW-1:0]       pn = p + {{(MW-1){1'b0}}, 1'b1};

    // ---- stage 2: first DFT layer ------------------------------------
    reg signed [AW-1:0] h_re [0:R-1];
    reg signed [AW-1:0] h_im [0:R-1];
    reg                 v2;

    // ---- stage 3 valid ----------------------------------------------
    reg                 vlast;

    assign out_valid = vlast && (synced || out_phase0);

    function signed [AW-1:0] ext;
        input signed [PW-1:0] v;
        begin
            ext = {{(AW-PW){v[PW-1]}}, v};
        end
    endfunction

    function signed [AW-1:0] rshift;
        input signed [AW-1:0] v;
        input integer         sh;
        begin
            if (sh <= 0)
                rshift = v;
            else
                rshift = (v + ($signed({{(AW-1){1'b0}}, 1'b1})
                                <<< (sh - 1))) >>> sh;
        end
    endfunction

    // round-half-up rescale by td, then saturate to OUT_WIDTH
    function signed [OW-1:0] rescale_sat;
        input signed [AW-1:0] v;
        reg   [AW-1:0]        t;
        begin
            t = rshift(v, RESHIFT);
            if (t[AW-1:OW-1] == {AW-OW+1{t[AW-1]}})
                rescale_sat = t[OW-1:0];              // in range
            else if (!t[AW-1])
                rescale_sat = {1'b0, {(OW-1){1'b1}}}; // +sat
            else
                rescale_sat = {1'b1, {(OW-1){1'b0}}}; // -sat
        end
    endfunction

    integer i;

    // stage 1: pre-twiddle -- EVERY lane scales by its W_N^{r*p}
    // (row 0 is all-ones in Q(td); skipping it would make that lane's
    // contribution 2^td times smaller than the others')
    genvar gp;
    generate
        for (gp = 0; gp < R; gp = gp + 1) begin : g_pre
            always @(posedge clk) begin
                if (rst) begin
                    wq_re[gp] <= {TWIDDLE_WIDTH{1'b0}};
                    wq_im[gp] <= {TWIDDLE_WIDTH{1'b0}};
                    d_re[gp]  <= {OW{1'b0}};
                    d_im[gp]  <= {OW{1'b0}};
                    b_re[gp]  <= {PW{1'b0}};
                    b_im[gp]  <= {PW{1'b0}};
                end else if (run) begin
                    // stage 1a: coefficient prefetch -- read with the
                    // NEXT slot phase; the registered coefficient pairs
                    // with the NEXT word's (delayed) data
                    wq_re[gp] <= w_re[gp];
                    wq_im[gp] <= w_im[gp];
                    d_re[gp]  <= din_re[gp*OW +: OW];
                    d_im[gp]  <= din_im[gp*OW +: OW];
                    // stage 1b: pre-twiddle products from the DELAYED
                    // operands and PREVIOUSLY fetched coefficient --
                    // both registered, keeping ROM+DSP off one cycle.
                    // Lane 0 (r = 0) is the identity twiddle: a plain
                    // left shift by td replaces the constant-coefficient
                    // multiply (differs from golden's 131071-multiply by
                    // <1 LSB, within documented SSR tolerance).
                    if (gp == 0) begin
                        b_re[gp] <= d_re[gp] <<< TWIDDLE_DECIMAL;
                        b_im[gp] <= d_im[gp] <<< TWIDDLE_DECIMAL;
                    end else begin
                        b_re[gp] <= $signed(d_re[gp]) * wq_re[gp]
                                  - $signed(d_im[gp]) * wq_im[gp];
                        b_im[gp] <= $signed(d_re[gp]) * wq_im[gp]
                                  + $signed(d_im[gp]) * wq_re[gp];
                    end
                end
            end
        end
    endgenerate

    always @(posedge clk) begin
        if (rst) begin
            p      <= {MW{1'b0}};
            // delay taps init to all-ones: never aliases the p == 0
            // sync phase before real data propagates
            pd     <= {MW{1'b1}};
            pd2    <= {MW{1'b1}};
            pd3    <= {MW{1'b1}};
            scnt   <= {(MW+3){1'b0}};
            synced <= 1'b0;
            v1     <= 1'b0;
            v2     <= 1'b0;
            vlast  <= 1'b0;
            // NOTE: wq_*/d_*/b_* are owned by the per-lane pre-twiddle
            // generate blocks below -- do not drive them here too
            // (multi-driven nets fold the whole datapath to constants)
            for (i = 0; i < R; i = i + 1) begin
                h_re[i]  <= {AW{1'b0}};  h_im[i] <= {AW{1'b0}};
            end
            dout_re <= {R*OW{1'b0}};
            dout_im <= {R*OW{1'b0}};
        end else if (run) begin
            // p free-runs gated by run: since the lanes also start
            // counting from their first valid word, p == the phase of
            // the word currently on din (word0 gets p = 0)
            p    <= p + {{(MW-1){1'b0}}, 1'b1};
            scnt <= scnt + {{(MW+3){1'b0}}, 1'b1};
            pd   <= p;
            pd2  <= pd;
            pd3  <= pd2;

            v1 <= 1'b1;

            // stage 2: lane-DFT layer(s), full precision
            if (R == 2) begin
                h_re[0] <= ext(b_re[0]) + ext(b_re[1]);
                h_im[0] <= ext(b_im[0]) + ext(b_im[1]);
                h_re[1] <= ext(b_re[0]) - ext(b_re[1]);
                h_im[1] <= ext(b_im[0]) - ext(b_im[1]);
            end else begin
                // R = 4: even/odd split
                h_re[0] <= ext(b_re[0]) + ext(b_re[2]);
                h_im[0] <= ext(b_im[0]) + ext(b_im[2]);
                h_re[1] <= ext(b_re[0]) - ext(b_re[2]);
                h_im[1] <= ext(b_im[0]) - ext(b_im[2]);
                h_re[2] <= ext(b_re[1]) + ext(b_re[3]);
                h_im[2] <= ext(b_im[1]) + ext(b_im[3]);
                h_re[3] <= ext(b_re[1]) - ext(b_re[3]);
                h_im[3] <= ext(b_im[1]) - ext(b_im[3]);
            end
            v2 <= v1;

            // stage 3: second layer (R=4) or identity (R=2), fused
            // rounding shift, rescale and saturate
            if (R == 2) begin
                dout_re[0*OW +: OW] <= rescale_sat(rshift(h_re[0], SX));
                dout_im[0*OW +: OW] <= rescale_sat(rshift(h_im[0], SX));
                dout_re[1*OW +: OW] <= rescale_sat(rshift(h_re[1], SX));
                dout_im[1*OW +: OW] <= rescale_sat(rshift(h_im[1], SX));
            end else begin
                // C0 = H0 + H2 ; C2 = H0 - H2
                dout_re[0*OW+:OW] <= rescale_sat(rshift(ext(h_re[0]) + ext(h_re[2]), SX));
                dout_im[0*OW+:OW] <= rescale_sat(rshift(ext(h_im[0]) + ext(h_im[2]), SX));
                dout_re[2*OW+:OW] <= rescale_sat(rshift(ext(h_re[0]) - ext(h_re[2]), SX));
                dout_im[2*OW+:OW] <= rescale_sat(rshift(ext(h_im[0]) - ext(h_im[2]), SX));
                // C1 = H1 - j*H3 ; C3 = H1 + j*H3
                dout_re[1*OW+:OW] <= rescale_sat(rshift(ext(h_re[1]) + ext(h_im[3]), SX));
                dout_im[1*OW+:OW] <= rescale_sat(rshift(ext(h_im[1]) - ext(h_re[3]), SX));
                dout_re[3*OW+:OW] <= rescale_sat(rshift(ext(h_re[1]) - ext(h_im[3]), SX));
                dout_im[3*OW+:OW] <= rescale_sat(rshift(ext(h_im[1]) + ext(h_re[3]), SX));
            end
            vlast <= v2;
            // frame-sync latch: first mature frame-start word starts
            // continuous emission
            synced <= synced || (v2 && out_phase0);
        end
    end

endmodule
`default_nettype wire
