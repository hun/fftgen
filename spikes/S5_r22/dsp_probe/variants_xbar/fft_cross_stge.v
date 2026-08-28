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
    parameter         WN_FILE    = "fft_wn.mem",   // R*M words, row r = W_N^{r*p}
    parameter         INVERSE    = 0               // lane-DFT direction
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
    // pipeline: input(q) -> fetch(wa/wq/d) -> products(pp) -> combine(b)
    //   -> DFT layer(s) -> half-shift(x) -> rescale/sat(dout)
    // R >= 8 inserts three more registered stages (G/H split, partial
    // layers + sqrt(2)/2 scalar products, odd-bin assembly).
    // The leading input register is load-bearing for TIMING: the lane
    // reorder buffers are depth-cascaded RAMB36E2 whose output register
    // has ~1.25 ns clock-to-out; with only ONE crossbar register before
    // the multiply, that register is absorbed into the DSP A/B port and
    // the BRAM clock-to-out shares a cycle with the DSP input setup
    // (post-synth WNS -0.14 at R=2 N=8192). Two registers ahead of the
    // multiply leave the first in fabric, splitting the path into two
    // short hops.
    localparam integer CB_LAT = (R >= 8) ? 11 : 7;

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

    // slot phase within a frame; pd.. delay it through the pipeline
    // stages so the tap at CB_LAT depth marks an output word whose
    // content word had phase p == 0
    reg [MW-1:0] p, pd, pd2, pd3, pd4, pd5, pd6, pd7, pd8, pd9, pd10,
                 pd11;
    reg [MW+3:0] scnt;
    reg          synced;
    wire         run = ce && in_valid;
    wire         mature = $unsigned(scnt) > (CB_LAT + 1);
    wire         out_phase0 = mature &&
                             ((R >= 8) ? (pd11 == 0) : (pd7 == 0));

    localparam integer OW = OUT_WIDTH;

    // ---- stage 0: input register (see CB_LAT comment) -----------------
    reg signed [OUT_WIDTH-1:0] q_re [0:R-1];
    reg signed [OUT_WIDTH-1:0] q_im [0:R-1];
    // ---- stage 1a: coefficient prefetch (registered ROM output) ------
    reg signed [TWIDDLE_WIDTH-1:0] wa_re [0:R-1];
    reg signed [TWIDDLE_WIDTH-1:0] wa_im [0:R-1];
    // ---- stage 1a2: coefficient second hop (pairs with d) ------------
    reg signed [TWIDDLE_WIDTH-1:0] wq_re [0:R-1];
    reg signed [TWIDDLE_WIDTH-1:0] wq_im [0:R-1];
    // ---- stage 1b operands: q delayed one cycle ----------------------
    reg signed [OUT_WIDTH-1:0] d_re [0:R-1];
    reg signed [OUT_WIDTH-1:0] d_im [0:R-1];
    // ---- stage 1b products (each maps to one DSP) --------------------
    // pp1 = dre*wre ; pp2 = dre*wim ; pp3 = dim*wre ; pp4 = dim*wim
    reg signed [AW-1:0] pp1 [0:R-1];
    reg signed [AW-1:0] pp2 [0:R-1];
    // the IM-operand products ride one DSP hop EARLY (from q/wa -- the
    // same value as d/wq one clock later) and are re-aligned to the
    // re-path products by the C-port regs below, so the combine sees a
    // matched MREG/C-port pair and the DSP keeps its MREG. Same two
    // levers as fft_stage_r22 (see its header): without them Vivado
    // merges the combine into the multiply's cycle (1.85 ns intra-DSP).
    reg signed [AW-1:0] pp3 [0:R-1];
    reg signed [AW-1:0] pp4 [0:R-1];
    reg signed [AW-1:0] pc3 [0:R-1];
    reg signed [AW-1:0] pc4 [0:R-1];
    // ---- stage 1c combine --------------------------------------------
    reg signed [AW-1:0] b_re [0:R-1];
    reg signed [AW-1:0] b_im [0:R-1];
    reg                 v1;
    wire [MW-1:0]       pn = p + {{(MW-1){1'b0}}, 1'b1};

    // ---- stage 2: first DFT layer ------------------------------------
    reg signed [AW-1:0] h_re [0:R-1];
    reg signed [AW-1:0] h_im [0:R-1];
    reg                 v2;

    // ---- stage 3a: fused s_x rounding shift --------------------------
    reg signed [AW-1:0] x_re [0:R-1];
    reg signed [AW-1:0] x_im [0:R-1];

    // ---- R = 8 lane-DFT stages ---------------------------------------
    // W_8 entries are {+/-1, +/-j} or (+/-sqrt2/2)(+/-1 +/- j), so the
    // 8-point lane DFT decomposes into add/sub/swap networks plus ONE
    // real scalar multiply by c8 = Q(td) sqrt(2)/2 per odd-q component.
    //   h1: G_r = B_r + B_{r+4}, H_r = B_r - B_{r+4}
    //   even q (=2k): 4-pt DFT of G (two trivial add layers)
    //   odd q: P = H0 -/+ jH2, Q = H1 -/+ jH3 (sign per INVERSE);
    //          U/V = sigma-signed Q; C_q = P + (U*c8 >>> td)
    localparam integer FPW = AW + TWIDDLE_WIDTH;
    localparam integer FLW = TWIDDLE_WIDTH + 18;   // lo product width
    localparam integer FHW = TWIDDLE_WIDTH + AW - 18 + 1; // hi product
    localparam real    C8_REAL = 0.7071067811865476 * (2 ** TWIDDLE_DECIMAL);
    localparam signed [TWIDDLE_WIDTH-1:0] C8 = $rtoi(C8_REAL + 0.5);
    reg signed [AW-1:0] e_re [0:3];       // even-q first-layer partials
    reg signed [AW-1:0] e_im [0:3];
    reg signed [AW-1:0] qq_re [0:3];      // Q_k (odd q = 2k+1) h2a
    reg signed [AW-1:0] qq_im [0:3];
    reg signed [AW-1:0] pp2_re [0:3];     // P_k h2a
    reg signed [AW-1:0] pp2_im [0:3];
    reg signed [AW-1:0] pq_re [0:3];      // P_k carried (h2b)
    reg signed [AW-1:0] pq_im [0:3];
    reg signed [AW-1:0] e3_re [0:3];      // even partials carried (h2b)
    reg signed [AW-1:0] e3_im [0:3];
    reg signed [AW-1:0] pe_re [0:3];      // P carried one more stage
    reg signed [AW-1:0] pe_im [0:3];
    reg signed [AW-1:0] pq2_re [0:3];     // P carried through prod stage
    reg signed [AW-1:0] pq2_im [0:3];
    reg signed [AW-1:0] e2_re [0:3];      // even partials carried (t3a)
    reg signed [AW-1:0] e2_im [0:3];
    // U * c8 with U wider than one DSP A-port: split into lo(18b)/hi
    // so each partial maps to ONE DSP48E2, then combine (registered).
    reg signed [FLW-1:0] flo_re [0:3];
    reg signed [FLW-1:0] flo_im [0:3];
    reg signed [FHW-1:0] fhi_re [0:3];
    reg signed [FHW-1:0] fhi_im [0:3];
    reg signed [FPW-1:0] f_re [0:3];      // combined U*c8 (registered)
    reg signed [FPW-1:0] f_im [0:3];
    reg signed [AW-1:0] ce_re [0:3];      // assembled even bins (q = 2k)
    reg signed [AW-1:0] ce_im [0:3];
    reg signed [AW-1:0] u_re [0:3];       // sigma-signed Q combos
    reg signed [AW-1:0] u_im [0:3];

    function signed [FPW-1:0] wide_round;
        // round-half-up >>>td in full product width
        input signed [FPW-1:0] v;
        begin
            wide_round = (v + ($signed({{(FPW-1){1'b0}}, 1'b1})
                                <<< (TWIDDLE_DECIMAL-1)))
                         >>> TWIDDLE_DECIMAL;
        end
    endfunction
    function signed [AW-1:0] shr_trunc;   // wide_round, truncate to AW
        input signed [FPW-1:0] v;
        begin
            shr_trunc = wide_round(v);
        end
    endfunction

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
    integer sig_k;
    // combinational temps for the R=8 h2 stage (blocking-assigned)
    reg signed [AW-1:0] tp_re, tp_im, tq_re, tq_im;

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
                    // NOTE: b_* are owned exclusively by the main block
                    // (combine stage) -- do not drive them here
                end else if (run) begin
                    // stage 0: input register (breaks the reorder-BRAM
                    // -> DSP path, see CB_LAT comment)
                    q_re[gp]  <= din_re[gp*OW +: OW];
                    q_im[gp]  <= din_im[gp*OW +: OW];
                    // stage 1a: coefficient prefetch -- the coefficient
                    // rides the SAME +1 delay as the data so the pair
                    // (word p, W^{r*p}) meets at the multiply
                    wa_re[gp] <= w_re[gp];
                    wa_im[gp] <= w_im[gp];
                    wq_re[gp] <= wa_re[gp];
                    wq_im[gp] <= wa_im[gp];
                    d_re[gp]  <= q_re[gp];
                    d_im[gp]  <= q_im[gp];
                    // stage 1b: partial products -- each maps to ONE
                    // DSP48E2 (registered at its MREG/PREG boundary).
                    // Lane 0 (r = 0) is the identity twiddle: a plain
                    // left shift by td replaces the constant-coefficient
                    // multiply (differs from golden's 131071-multiply by
                    // <1 LSB, within documented SSR tolerance).
                    // im-operand products from the EARLIER operand hop
                    if (gp == 0) begin
                        pp3[gp] <= q_im[gp] <<< TWIDDLE_DECIMAL;
                        pp4[gp] <= {AW{1'b0}};
                    end else begin
                        pp3[gp] <= q_im[gp] * wa_re[gp];
                        pp4[gp] <= q_im[gp] * wa_im[gp];
                    end
                    // re-operand products (MREG) + the im products at the
                    // combine DSPs' C ports (CREG)
                    if (gp == 0) begin
                        pp1[gp] <= d_re[gp] <<< TWIDDLE_DECIMAL;
                        pp2[gp] <= {AW{1'b0}};
                    end else begin
                        pp1[gp] <= d_re[gp] * wq_re[gp];
                        pp2[gp] <= d_re[gp] * wq_im[gp];
                    end
                    pc3[gp] <= pp3[gp];
                    pc4[gp] <= pp4[gp];
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
            pd4    <= {MW{1'b1}};
            pd5    <= {MW{1'b1}};
            pd6    <= {MW{1'b1}};
            pd7    <= {MW{1'b1}};
            pd8    <= {MW{1'b1}};
            pd9    <= {MW{1'b1}};
            pd10   <= {MW{1'b1}};
            pd11   <= {MW{1'b1}};
            scnt   <= {(MW+3){1'b0}};
            synced <= 1'b0;
            v1     <= 1'b0;
            v2     <= 1'b0;
            vlast  <= 1'b0;
            // NOTE: wq_*/d_*/b_* are owned by the per-lane pre-twiddle
            // generate blocks below -- do not drive them here too
            // (multi-driven nets fold the whole datapath to constants)
            for (i = 0; i < R; i = i + 1) begin
                b_re[i]  <= {AW{1'b0}};  b_im[i] <= {AW{1'b0}};
                h_re[i]  <= {AW{1'b0}};  h_im[i] <= {AW{1'b0}};
                x_re[i]  <= {AW{1'b0}};  x_im[i] <= {AW{1'b0}};
            end
            for (i = 0; i < 4; i = i + 1) begin
                e_re[i]  <= {AW{1'b0}};  e_im[i]  <= {AW{1'b0}};
                pq_re[i] <= {AW{1'b0}};  pq_im[i] <= {AW{1'b0}};
                pe_re[i] <= {AW{1'b0}};  pe_im[i] <= {AW{1'b0}};
                ce_re[i] <= {AW{1'b0}};  ce_im[i] <= {AW{1'b0}};
                u_re[i]  <= {AW{1'b0}};  u_im[i]  <= {AW{1'b0}};
                f_re[i]  <= {FPW{1'b0}}; f_im[i]  <= {FPW{1'b0}};
                flo_re[i]<= {FLW{1'b0}}; flo_im[i] <= {FLW{1'b0}};
                fhi_re[i]<= {FHW{1'b0}}; fhi_im[i] <= {FHW{1'b0}};
                pq2_re[i]<= {AW{1'b0}}; pq2_im[i] <= {AW{1'b0}};
                e2_re[i] <= {AW{1'b0}}; e2_im[i]  <= {AW{1'b0}};
                qq_re[i] <= {AW{1'b0}}; qq_im[i]  <= {AW{1'b0}};
                pp2_re[i]<= {AW{1'b0}}; pp2_im[i] <= {AW{1'b0}};
                e3_re[i] <= {AW{1'b0}}; e3_im[i]  <= {AW{1'b0}};
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
            pd4  <= pd3;
            pd5  <= pd4;
            pd6  <= pd5;
            pd7  <= pd6;
            pd8  <= pd7;
            pd9  <= pd8;
            pd10 <= pd9;
            pd11 <= pd10;

            v1 <= 1'b1;

            // stage 2a: combine partial products -> complex bins
            // B_r = (pp1 - pp4) + j*(pp2 + pp3)
            for (i = 0; i < R; i = i + 1) begin
                b_re[i] <= $signed(ext(pp1[i])) - $signed(ext(pc4[i]));
                b_im[i] <= $signed(ext(pp2[i])) + $signed(ext(pc3[i]));
            end

            // stage 2b: lane-DFT layer(s), full precision
            // uses PREVIOUS b (one cycle behind pp) ✓ correct pairing
            if (R == 2) begin
                h_re[0] <= ext(b_re[0]) + ext(b_re[1]);
                h_im[0] <= ext(b_im[0]) + ext(b_im[1]);
                h_re[1] <= ext(b_re[0]) - ext(b_re[1]);
                h_im[1] <= ext(b_im[0]) - ext(b_im[1]);
            end else if (R == 4) begin
                // even/odd split
                h_re[0] <= ext(b_re[0]) + ext(b_re[2]);
                h_im[0] <= ext(b_im[0]) + ext(b_im[2]);
                h_re[1] <= ext(b_re[0]) - ext(b_re[2]);
                h_im[1] <= ext(b_im[0]) - ext(b_im[2]);
                h_re[2] <= ext(b_re[1]) + ext(b_re[3]);
                h_im[2] <= ext(b_im[1]) + ext(b_im[3]);
                h_re[3] <= ext(b_re[1]) - ext(b_re[3]);
                h_im[3] <= ext(b_im[1]) - ext(b_im[3]);
            end else begin
                // R = 8: G/H split, G in h[0..3], H in h[4..7]
                for (i = 0; i < 4; i = i + 1) begin
                    h_re[i]   <= ext(b_re[i]) + ext(b_re[i+4]);
                    h_im[i]   <= ext(b_im[i]) + ext(b_im[i+4]);
                    h_re[i+4] <= ext(b_re[i]) - ext(b_re[i+4]);
                    h_im[i+4] <= ext(b_im[i]) - ext(b_im[i+4]);
                end
            end
            v2 <= v1;

            if (R >= 8) begin
                // ---- R=8 stage h2: even partials e, odd P/Q/U --------
                // even q: first 2-pt layer of the 4-pt DFT of G
                // NOTE: h_* are AW-wide; ext() would truncate the top
                // 5 bits when |H| > 2^33 -- use them directly
                e_re[0] <= h_re[0] + h_re[2];
                e_im[0] <= h_im[0] + h_im[2];
                e_re[1] <= h_re[0] - h_re[2];
                e_im[1] <= h_im[0] - h_im[2];
                e_re[2] <= h_re[1] + h_re[3];
                e_im[2] <= h_im[1] + h_im[3];
                e_re[3] <= h_re[1] - h_re[3];
                e_im[3] <= h_im[1] - h_im[3];
                // odd q = 2k+1: C_q = P_k + W_8^q * Q_k where the inner
                // pair factor W_8^{2q} ALTERNATES (-j for k even, +j for
                // k odd in FORWARD; conjugate under INVERSE), so P/Q use
                // per-k +/-j pairings of H0..H3.
                // h2a registers Q and P (one add level from H); the
                // sigma-signed U combos move to h2b so at most one
                // 39-bit add sits before the DSP multiply.
                for (i = 0; i < 4; i = i + 1) begin
                    if ((i[0] ^ (INVERSE != 0)) == 0) begin  // -j pairing
                        tp_re = h_re[4] + h_im[6];
                        tp_im = h_im[4] - h_re[6];
                        tq_re = h_re[5] + h_im[7];
                        tq_im = h_im[5] - h_re[7];
                    end else begin        // q=3,7: +j pairing
                        tp_re = h_re[4] - h_im[6];
                        tp_im = h_im[4] + h_re[6];
                        tq_re = h_re[5] - h_im[7];
                        tq_im = h_im[5] + h_re[7];
                    end
                    pp2_re[i] <= tp_re;
                    pp2_im[i] <= tp_im;
                    qq_re[i]  <= tq_re;
                    qq_im[i]  <= tq_im;
                end
            end

            if (R >= 8) begin
                // ---- R=8 stage h2b: sigma-signed U, P/e carries ------
                for (i = 0; i < 4; i = i + 1) begin
                    // forward sc/ss per q=2k+1: (1,-1) (-1,-1) (-1,1) (1,1);
                    // inverse mirrors k->3-k
                    sig_k = (INVERSE != 0) ? (3 - i) : i;
                    case (sig_k)
                        0: begin
                            u_re[i] <= qq_re[i] + qq_im[i];
                            u_im[i] <= qq_im[i] - qq_re[i];
                        end
                        1: begin
                            u_re[i] <= qq_im[i] - qq_re[i];
                            u_im[i] <= -(qq_im[i] + qq_re[i]);
                        end
                        2: begin
                            u_re[i] <= -(qq_re[i] + qq_im[i]);
                            u_im[i] <= qq_re[i] - qq_im[i];
                        end
                        3: begin
                            u_re[i] <= qq_re[i] - qq_im[i];
                            u_im[i] <= qq_re[i] + qq_im[i];
                        end
                    endcase
                    pq_re[i] <= pp2_re[i];
                    pq_im[i] <= pp2_im[i];
                    e3_re[i] <= e_re[i];
                    e3_im[i] <= e_im[i];
                end
            end

            if (R >= 8) begin
                // ---- R=8 stage t3a: split scalar products (DSPs) ----
                for (i = 0; i < 4; i = i + 1) begin
                    // u = u_hi*2^18 + u_lo (signed/unsigned split); each
                    // partial fits one DSP48E2 (18x18 and ~21x18).
                    // low 18 bits are UNSIGNED (part-select of a signed
                    // vector is unsigned; $signed() would mis-sign it)
                    flo_re[i] <= $unsigned(u_re[i][17:0]) * C8;
                    flo_im[i] <= $unsigned(u_im[i][17:0]) * C8;
                    fhi_re[i] <= ($signed(u_re[i]) >>> 18) * C8;
                    fhi_im[i] <= ($signed(u_im[i]) >>> 18) * C8;
                    // P and even partials ride the product latency
                    pq2_re[i] <= pq_re[i];
                    pq2_im[i] <= pq_im[i];
                end
                for (i = 0; i < 4; i = i + 1) begin
                    e2_re[i] <= e3_re[i];
                    e2_im[i] <= e3_im[i];
                end
            end

            if (R >= 8) begin
                // ---- R=8 stage t3b: combine products, P, even bins --
                for (i = 0; i < 4; i = i + 1) begin
                    f_re[i] <= ($signed({ { (FPW-FHW){fhi_re[i][FHW-1]}},
                                          fhi_re[i]}) <<< 18) + flo_re[i];
                    f_im[i] <= ($signed({ { (FPW-FHW){fhi_im[i][FHW-1]}},
                                          fhi_im[i]}) <<< 18) + flo_im[i];
                    pe_re[i] <= pq2_re[i];  // final P (one stage later)
                    pe_im[i] <= pq2_im[i];
                end
                ce_re[0] <= e2_re[0] + e2_re[2];   // q = 0
                ce_im[0] <= e2_im[0] + e2_im[2];
                ce_re[2] <= e2_re[0] - e2_re[2];   // q = 4
                ce_im[2] <= e2_im[0] - e2_im[2];
                if (INVERSE != 0) begin
                    // q = 2: e1 + j e3 ; q = 6: e1 - j e3
                    ce_re[1] <= e2_re[1] - e2_im[3];
                    ce_im[1] <= e2_im[1] + e2_re[3];
                    ce_re[3] <= e2_re[1] + e2_im[3];
                    ce_im[3] <= e2_im[1] - e2_re[3];
                end else begin
                    // q = 2: e1 - j e3 ; q = 6: e1 + j e3
                    ce_re[1] <= e2_re[1] + e2_im[3];
                    ce_im[1] <= e2_im[1] - e2_re[3];
                    ce_re[3] <= e2_re[1] - e2_im[3];
                    ce_im[3] <= e2_im[1] + e2_re[3];
                end
            end

            // stage 3a: second DFT layer (R=4) or identity (R=2),
            // fused s_x rounding shift -- registered so the carry
            // chain from the layer adders ends here
            if (R == 2) begin
                x_re[0] <= rshift(h_re[0], SX);
                x_im[0] <= rshift(h_im[0], SX);
                x_re[1] <= rshift(h_re[1], SX);
                x_im[1] <= rshift(h_im[1], SX);
            end else if (R == 4) begin
                // C0 = H0 + H2 ; C2 = H0 - H2
                x_re[0] <= rshift(ext(h_re[0]) + ext(h_re[2]), SX);
                x_im[0] <= rshift(ext(h_im[0]) + ext(h_im[2]), SX);
                x_re[2] <= rshift(ext(h_re[0]) - ext(h_re[2]), SX);
                x_im[2] <= rshift(ext(h_im[0]) - ext(h_im[2]), SX);
                // C1 = H1 -/+ j*H3 ; C3 = H1 +/- j*H3 (direction-dep)
                if (INVERSE != 0) begin
                    x_re[1] <= rshift(ext(h_re[1]) - ext(h_im[3]), SX);
                    x_im[1] <= rshift(ext(h_im[1]) + ext(h_re[3]), SX);
                    x_re[3] <= rshift(ext(h_re[1]) + ext(h_im[3]), SX);
                    x_im[3] <= rshift(ext(h_im[1]) - ext(h_re[3]), SX);
                end else begin
                    x_re[1] <= rshift(ext(h_re[1]) + ext(h_im[3]), SX);
                    x_im[1] <= rshift(ext(h_im[1]) - ext(h_re[3]), SX);
                    x_re[3] <= rshift(ext(h_re[1]) - ext(h_im[3]), SX);
                    x_im[3] <= rshift(ext(h_im[1]) + ext(h_re[3]), SX);
                end
            end else begin
                // R = 8: assemble odd bins, pass even bins through,
                // apply the fused s_x rounding shift to everything
                for (i = 0; i < 4; i = i + 1) begin
                    x_re[2*i]   <= rshift(ce_re[i], SX);
                    x_im[2*i]   <= rshift(ce_im[i], SX);
                    x_re[2*i+1] <= rshift(pe_re[i]
                                           + shr_trunc(f_re[i]), SX);
                    x_im[2*i+1] <= rshift(pe_im[i]
                                           + shr_trunc(f_im[i]), SX);
                end
            end
            // stage 3b: rescale Q(od+td) -> Q(od), saturate to OW
            for (i = 0; i < R; i = i + 1) begin
                dout_re[i*OW +: OW] <= rescale_sat(x_re[i]);
                dout_im[i*OW +: OW] <= rescale_sat(x_im[i]);
            end
            vlast <= v2;
            // frame-sync latch: first mature frame-start word starts
            // continuous emission
            synced <= synced || (v2 && out_phase0);
        end
    end

endmodule
`default_nettype wire
