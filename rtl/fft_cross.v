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
//   * pre-twiddle products kept FULL precision (ow + tw bits)
//   * accumulation exact (ow + 2*tw + log2(R) bits)
//   * one fused rounding shift s_x = log2(R)
//   * round-half-up rescale Q(od + 2*td) -> Q(od), saturate to ow
//
// Emission is frame-synced: words are dropped until the first p == 0
// slot at or after the pipeline has filled, then continuously valid.
//
// v1 implements R = 2 (one pre-twiddle complex multiply, one butterfly);
// larger R unrolls into radix-2 layers at generation time.
`default_nettype none

module fft_cross #(
    parameter integer NUM_POINTS = 8,        // full N
    parameter integer SSR        = 2,        // R
    parameter integer OUT_WIDTH  = 16,
    parameter integer OUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH = 18,
    parameter integer TWIDDLE_DECIMAL = 17,
    parameter         WN_FILE    = "fft_wn.mem"   // (R-1)*M words, r=1..R-1
)(
    input  wire                        clk,
    input  wire                        ce,
    input  wire                        rst,
    input  wire                        in_valid,
    input  wire signed [OUT_WIDTH-1:0] din_re [0:SSR-1],
    input  wire signed [OUT_WIDTH-1:0] din_im [0:SSR-1],

    output wire                        out_valid,
    output reg  signed [OUT_WIDTH-1:0] dout_re [0:SSR-1],
    output reg  signed [OUT_WIDTH-1:0] dout_im [0:SSR-1]
);

    localparam integer N  = NUM_POINTS;
    localparam integer R  = SSR;
    localparam integer M  = N / R;
    localparam integer MW = (M > 1) ? $clog2(M) : 1;
    localparam integer PW = OUT_WIDTH + TWIDDLE_WIDTH;
    localparam integer AW = PW + TWIDDLE_WIDTH + 2;
    localparam integer SX = $clog2(R);
    // words to skip before an output can be trusted (pipeline fill)
    localparam integer FILL = 3;
    // the lane-DFT coefficients W_R^{rq} are applied exactly ({0,+/-1,
    // +/-j} for R <= 4), so only the pre-twiddle's td scales the chain
    localparam integer RESHIFT = TWIDDLE_DECIMAL;

    // pre-twiddle ROM: R rows x M columns; row r holds W_N^{r*p}
    // INCLUDING r = 0 (W = 1.0 in Q(td)) -- every lane must be scaled
    // identically or the butterfly sums cancel one lane entirely
    (* ram_style = "distributed" *)
    reg signed [TWIDDLE_WIDTH*2-1:0] wn_rom [0:R*M-1];
    initial begin
        $readmemh(WN_FILE, wn_rom);
    end

    wire signed [TWIDDLE_WIDTH-1:0] w_re =
        wn_rom[M + p][TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
    wire signed [TWIDDLE_WIDTH-1:0] w_im = wn_rom[M + p][TWIDDLE_WIDTH-1:0];
    wire signed [TWIDDLE_WIDTH-1:0] w0_re =
        wn_rom[p][TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
    wire signed [TWIDDLE_WIDTH-1:0] w0_im = wn_rom[p][TWIDDLE_WIDTH-1:0];

    // p counts words since the first lane-valid word (phase reference =
    // the lanes themselves), then runs freely; pd/pd2 delay p through
    // the two pipeline stages so pd2 == 0 marks an output word that is
    // a frame start (p == 0). scnt counts those words for maturity.
    reg [MW-1:0] p, pd, pd2;
    reg [MW:0]   scnt;
    reg          synced;
    wire         run = ce && in_valid;
    wire         mature  = $unsigned(scnt) > FILL[MW:0];
    wire         synced_phase0 = mature && (pd2 == {{(MW-1){1'b0}}, 1'b0});
    assign out_valid = v2 && (synced || (mature && (pd2 == {{(MW-1){1'b0}}, 1'b0})));

    // ---- stage 1: pre-twiddle products (full precision registers) ----
    reg signed [PW-1:0] b0_re, b0_im;
    reg signed [PW-1:0] b1_re, b1_im;
    reg                 v1;

    // ---- stage 2: butterfly + fused rounding shift + rescale --------
    localparam integer OW = OUT_WIDTH;
    reg signed [AW-1:0] tmp;
    reg                 v2;

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

    // round-half-up rescale by 2*td, then saturate to OUT_WIDTH
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
    always @(posedge clk) begin
        if (rst) begin
            p      <= {MW{1'b0}};
            // delay taps init to all-ones: never aliases the p == 0
            // sync phase before real data propagates
            pd     <= {MW{1'b1}};
            pd2    <= {MW{1'b1}};
            scnt   <= {(MW+1){1'b0}};
            synced <= 1'b0;
            v1     <= 1'b0;
            v2     <= 1'b0;
            b0_re  <= {PW{1'b0}};  b0_im <= {PW{1'b0}};
            b1_re  <= {PW{1'b0}};  b1_im <= {PW{1'b0}};
            for (i = 0; i < R; i = i + 1) begin
                dout_re[i] <= {OW{1'b0}};
                dout_im[i] <= {OW{1'b0}};
            end
        end else if (run) begin
            // p free-runs from reset gated by run: since the lanes also
            // start counting from their first valid word, p == the phase
            // of the word currently on din (word0 gets p = 0)
            p    <= p + {{(MW-1){1'b0}}, 1'b1};
            scnt <= scnt + {{MW{1'b0}}, 1'b1};
            pd   <= p;
            pd2  <= pd;

            // stage 1: pre-twiddle -- EVERY lane scales by its W_N^{r*p}
            // (row 0 is all-ones in Q(td); skipping it would make lane 0's
            // contribution 2^td times smaller than lane 1's)
            b0_re <= din_re[0] * w0_re - din_im[0] * w0_im;
            b0_im <= din_re[0] * w0_im + din_im[0] * w0_re;
            b1_re <= din_re[1] * w_re - din_im[1] * w_im;
            b1_im <= din_re[1] * w_im + din_im[1] * w_re;
            v1    <= 1'b1;

            // stage 2: butterfly + fused shift + rescale
            // q = 0 : B0 + B1
            dout_re[0] <= rescale_sat(rshift(
                ({{(AW-PW){b0_re[PW-1]}}, b0_re}
                 + {{(AW-PW){b1_re[PW-1]}}, b1_re}), SX));
            dout_im[0] <= rescale_sat(rshift(
                ({{(AW-PW){b0_im[PW-1]}}, b0_im}
                 + {{(AW-PW){b1_im[PW-1]}}, b1_im}), SX));
            // q = 1 : B0 - B1
            dout_re[1] <= rescale_sat(rshift(
                ({{(AW-PW){b0_re[PW-1]}}, b0_re}
                 - {{(AW-PW){b1_re[PW-1]}}, b1_re}), SX));
            dout_im[1] <= rescale_sat(rshift(
                ({{(AW-PW){b0_im[PW-1]}}, b0_im}
                 - {{(AW-PW){b1_im[PW-1]}}, b1_im}), SX));
            if (v1 || v2)
                $display("[XB p=%0d] b0=(%0d,%0d) b1=(%0d,%0d) dout0=(%0d,%0d)",
                         p, b0_re, b0_im, b1_re, b1_im,
                         dout_re[0], dout_im[0]);
            v2 <= v1;
            // frame-sync: first p == 0 word after the pipe fills starts
            // the emission stream; afterwards every word emits
            synced <= synced || (v2 && synced_phase0);
        end
    end

endmodule
`default_nettype wire
