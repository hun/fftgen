// fftgen -- streaming radix-2 DIF SDF FFT core (R = 1)
//
// Generated-core template; the Python generator binds all parameters and
// supplies the twiddle ROM contents (fft_twiddles.mem). Bit-exact contract:
// src/golden.py SDFGoldenModel (see PLAN.md appendix A).
//
// Interface: AXI4-Stream-encoded minus tready (PLAN.md 2.8) -- free-running
// pipeline gated by ce && s_axis_tvalid, frame sidebands ride at fixed
// latency. No resets on datapath registers; control state uses synchronous
// reset only (PLAN.md 2.9).

`default_nettype none

module fft_sdf #(
    // N, power of two, >= 2
    parameter integer NUM_POINTS     = 16,
    // signed component widths
    parameter integer SAMPLE_WIDTH   = 16,
    parameter integer SAMPLE_DECIMAL = 0,
    parameter integer OUTPUT_WIDTH   = 16,
    parameter integer OUTPUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    // per-stage right-shift schedule, 2 bits per stage, stage 0 in LSBs
    parameter integer SCALING_PACK   = 32'h01010101,
    // twiddle table file: NUM_POINTS words, {re, im} signed MSB:LSB;
    // stage s occupies entries [BASE_s .. BASE_s + D_s - 1],
    // BASE_s = sum(D_t, t < s), D_s = NUM_POINTS >> (s+1)
    parameter TWIDDLE_FILE           = "fft_twiddles.mem",
    // internal growth headroom, generator-derived:
    // SAMPLE_WIDTH + max(0, num_stages - total_shift) + 1
    parameter integer INTERN_WIDTH   = SAMPLE_WIDTH + 5
)(
    input  wire                      clk,
    input  wire                      ce,
    input  wire                      s_axis_tvalid,  // advances on ce && tvalid
    input  wire [SAMPLE_WIDTH-1:0]   s_axis_tdata_re,
    input  wire [SAMPLE_WIDTH-1:0]   s_axis_tdata_im,
    input  wire                      s_axis_tuser,   // start of frame
    input  wire                      s_axis_tlast,   // end of frame

    output wire                      m_axis_tvalid,
    output wire [OUTPUT_WIDTH-1:0]   m_axis_tdata_re,
    output wire [OUTPUT_WIDTH-1:0]   m_axis_tdata_im,
    output wire                      m_axis_tuser,
    output wire                      m_axis_tlast,

    input  wire                      rst             // sync, control only
);

    localparam integer N       = NUM_POINTS;
    localparam integer NSTAGES = $clog2(N);
    localparam integer LATENCY = N + NSTAGES;
    localparam integer CNT_W   = $clog2(LATENCY + 1);
    localparam integer AROM_W  = $clog2(N);

    // ------------------------------------------------------------------
    // advance condition + cycle counter (control: sync reset)
    // ------------------------------------------------------------------
    wire run = ce && s_axis_tvalid;

    reg [CNT_W-1:0] cnt;
    reg             out_valid_r;

    always @(posedge clk) begin
        if (rst) begin
            cnt          <= {CNT_W{1'b0}};
            out_valid_r  <= 1'b0;
        end else if (run) begin
            if (cnt != LATENCY[CNT_W-1:0])
                cnt <= cnt + {{(CNT_W-1){1'b0}}, 1'b1};
            // latch once the pipeline is full; hold thereafter (stable
            // under freeze)
            if (cnt == LATENCY[CNT_W-1:0] - 1'b1)
                out_valid_r <= 1'b1;
        end
        // frozen cycles: everything holds
    end

    // contract (PLAN.md 2.8): tvalid is low whenever the datapath is
    // frozen (run == 0), so a consumer never sees stale data twice
    assign m_axis_tvalid = out_valid_r && run;

    // ------------------------------------------------------------------
    // frame marker sideband: shift register, depth LATENCY
    // ------------------------------------------------------------------
    reg mk_user [0:LATENCY-1];
    reg mk_last [0:LATENCY-1];
    integer k;

    always @(posedge clk) begin
        if (run) begin
            mk_user[0] <= s_axis_tuser;
            mk_last[0] <= s_axis_tlast;
            for (k = 1; k < LATENCY; k = k + 1) begin
                mk_user[k] <= mk_user[k-1];
                mk_last[k] <= mk_last[k-1];
            end
        end
    end

    assign m_axis_tuser = mk_user[LATENCY-1];
    assign m_axis_tlast = mk_last[LATENCY-1];

    // ------------------------------------------------------------------
    // stage chain (one pipeline register per stage, inside fft_stage)
    // ------------------------------------------------------------------
    wire signed [INTERN_WIDTH-1:0] st_out_re [0:NSTAGES-1];
    wire signed [INTERN_WIDTH-1:0] st_out_im [0:NSTAGES-1];

    genvar g;
    generate
        for (g = 0; g < NSTAGES; g = g + 1) begin : stages
            localparam integer DEPTH     = N >> (g + 1);
            localparam integer SHIFT     = (SCALING_PACK >> (2*g)) & 3;
            localparam integer SUM_D     = N - (N >> g);   // sum(D_t, t<g)
            // FSM alignment preload (appendix A):
            //   warm_s = -(SUM_D + s) mod 2*D_s
            localparam integer WARM      =
                (((2*DEPTH) - ((SUM_D + g) % (2*DEPTH))) % (2*DEPTH));
            localparam integer PRELOAD_I = WARM % DEPTH;
            localparam        PRELOAD_C = (WARM >= DEPTH) ? 1 : 0;

            wire signed [TWIDDLE_WIDTH*2-1:0] rom_word;
            wire [AROM_W-1:0]                 rom_addr_w;

            fft_stage #(
                .DEPTH          (DEPTH),
                .WIDTH          (INTERN_WIDTH),
                .SHIFT          (SHIFT),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .K_STRIDE       (1 << g),
                .ROM_BASE       (SUM_D),
                .NPTS           (N),
                .PRELOAD_I      (PRELOAD_I),
                .PRELOAD_C      (PRELOAD_C)
            ) u_stage (
                .clk      (clk),
                .ce       (run),
                .rst      (rst),
                .in_re    (g == 0 ?
                    $signed({{(INTERN_WIDTH-SAMPLE_WIDTH){s_axis_tdata_re[SAMPLE_WIDTH-1]}},
                              s_axis_tdata_re}) :
                    $signed(st_out_re[g-1])),
                .in_im    (g == 0 ?
                    $signed({{(INTERN_WIDTH-SAMPLE_WIDTH){s_axis_tdata_im[SAMPLE_WIDTH-1]}},
                              s_axis_tdata_im}) :
                    $signed(st_out_im[g-1])),
                .out_re   (st_out_re[g]),
                .out_im   (st_out_im[g]),
                .rom_addr (rom_addr_w),
                .rom_data (rom_word)
            );

            assign rom_word = tw_rom[rom_addr_w];
        end
    endgenerate

    // ------------------------------------------------------------------
    // twiddle ROM (single array, async read; generator supplies contents)
    // ------------------------------------------------------------------
    (* ram_style = "distributed" *)
    reg signed [TWIDDLE_WIDTH*2-1:0] tw_rom [0:N-1];

    initial begin
        $readmemh(TWIDDLE_FILE, tw_rom);
    end

    // ------------------------------------------------------------------
    // output quantization: rescale Q(sd) -> Q(od), saturate to OUTPUT_WIDTH
    // (matches quant.quantize_output exactly)
    // ------------------------------------------------------------------
    localparam integer RESHIFT =
        (SAMPLE_DECIMAL > OUTPUT_DECIMAL) ? SAMPLE_DECIMAL - OUTPUT_DECIMAL : 0;
    // extended comparison domain: internal width + rescale + output width
    // + safety (must cover the saturated extremes in the output domain)
    localparam integer QW = INTERN_WIDTH + RESHIFT + OUTPUT_WIDTH + 2;

    function [OUTPUT_WIDTH-1:0] quant_out;
        input signed [INTERN_WIDTH-1:0] v;
        reg signed [QW-1:0] t;
        reg signed [QW-1:0] hi, lo;      // extended-domain bounds
        reg [OUTPUT_WIDTH-1:0] ohi, olo; // output-domain extremes
        begin
            ohi = {{1'b0}, {(OUTPUT_WIDTH-1){1'b1}}};                 // 2^(OW-1)-1
            olo = {{1'b1}, {(OUTPUT_WIDTH-1){1'b0}}};             // -2^(OW-1)
            hi  = {{(QW-OUTPUT_WIDTH){1'b0}}, ohi};               // sign-extend
            lo  = {{(QW-OUTPUT_WIDTH){1'b1}}, olo};
            if (SAMPLE_DECIMAL > OUTPUT_DECIMAL) begin
                t = ($signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}}, v})
                     + ($signed({{(QW-1){1'b0}}, 1'b1})
                        <<< (RESHIFT - 1))) >>> RESHIFT;
            end else if (SAMPLE_DECIMAL < OUTPUT_DECIMAL) begin
                t = $signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}}, v})
                    <<< (OUTPUT_DECIMAL - SAMPLE_DECIMAL);
            end else begin
                t = $signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}}, v});
            end
            if (t > hi)
                quant_out = ohi;
            else if (t < lo)
                quant_out = olo;
            else
                quant_out = t[OUTPUT_WIDTH-1:0];
        end
    endfunction

    reg [OUTPUT_WIDTH-1:0] m_re_r, m_im_r;
    always @(posedge clk) begin
        if (run) begin
            m_re_r <= quant_out(st_out_re[NSTAGES-1]);
            m_im_r <= quant_out(st_out_im[NSTAGES-1]);
        end
    end
    assign m_axis_tdata_re = m_re_r;
    assign m_axis_tdata_im = m_im_r;

endmodule


// ----------------------------------------------------------------------
// one radix-2 DIF SDF stage: delay line + butterfly + complex multiply
// ----------------------------------------------------------------------
module fft_stage #(
    parameter integer DEPTH          = 4,
    parameter integer WIDTH          = 16,   // internal sample width
    parameter integer SHIFT          = 1,    // per-stage scaling shift 0..2
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL= 17,
    parameter integer K_STRIDE       = 1,    // twiddle exponent stride (2^s)
    parameter integer ROM_BASE       = 0,
    parameter integer NPTS           = 16,
    parameter integer PRELOAD_I      = 0,    // FSM alignment preload
    parameter         PRELOAD_C      = 0     // start in COMPUTE phase
)(
    input  wire             clk,
    input  wire             ce,
    input  wire             rst,
    input  wire signed [WIDTH-1:0] in_re,
    input  wire signed [WIDTH-1:0] in_im,
    output reg  signed [WIDTH-1:0] out_re,
    output reg  signed [WIDTH-1:0] out_im,
    // twiddle read port (combinational): address valid during COMPUTE
    output wire [$clog2(NPTS)-1:0] rom_addr,
    input  wire signed [TWIDDLE_WIDTH*2-1:0] rom_data
);

    localparam AW = (DEPTH > 1) ? $clog2(DEPTH) : 1;
    localparam AROM_W = $clog2(NPTS);
    localparam [AW-1:0] DEPTH_M1 = DEPTH[AW-1:0] - 1'b1;
    localparam [AW-1:0] DEPTH_MASK = DEPTH[AW-1:0] - 1'b1;
    localparam integer TD_PLUS_SHIFT = TWIDDLE_DECIMAL + SHIFT;
    // product width: (d-a) is WIDTH+1 bits, (cr+ci) is TW+1 bits, plus slack
    localparam integer PW = WIDTH + TWIDDLE_WIDTH + 4;

    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] mem_re [0:DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] mem_im [0:DEPTH-1];

    reg [AW-1:0] ptr;
    reg          in_compute;   // FSM phase: 0 = PASS/FILL, 1 = COMPUTE
    reg [AW-1:0] phase_i;      // pair index within phase

    wire signed [WIDTH-1:0] d_re = mem_re[ptr];
    wire signed [WIDTH-1:0] d_im = mem_im[ptr];

    // twiddle decode {re, im}
    wire signed [TWIDDLE_WIDTH-1:0] t_re = rom_data[TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
    wire signed [TWIDDLE_WIDTH-1:0] t_im = rom_data[TWIDDLE_WIDTH-1:0];

    // absolute twiddle address for current pair
    // pair index i = phase_i masked to the phase length (for DEPTH==1 this
    // stays 0 -- the phase counter cycles but the pair index must not).
    // ROM entries are PRE-PERMUTED by the generator (T[(i<<s)%N]), so the
    // address is contiguous: BASE + pair_i.
    wire [AW-1:0] pair_i = phase_i & DEPTH_MASK;
    assign rom_addr = ROM_BASE[AROM_W-1:0]
                      + {{(AROM_W-AW){1'b0}}, pair_i};

    // sign-extended working copies
    wire signed [PW-1:0] w_in_re = {{(PW-WIDTH){in_re[WIDTH-1]}}, in_re};
    wire signed [PW-1:0] w_in_im = {{(PW-WIDTH){in_im[WIDTH-1]}}, in_im};
    wire signed [PW-1:0] w_d_re  = {{(PW-WIDTH){d_re[WIDTH-1]}},  d_re};
    wire signed [PW-1:0] w_d_im  = {{(PW-WIDTH){d_im[WIDTH-1]}},  d_im};
    wire signed [PW-1:0] w_t_re  = {{(PW-TWIDDLE_WIDTH){t_re[TWIDDLE_WIDTH-1]}}, t_re};
    wire signed [PW-1:0] w_t_im  = {{(PW-TWIDDLE_WIDTH){t_im[TWIDDLE_WIDTH-1]}}, t_im};

    // round-half-up arithmetic right shift (quant.round_shift)
    function signed [PW-1:0] round_shift;
        input signed [PW-1:0] v;
        input integer         sh;
        begin
            if (sh <= 0)
                round_shift = v;
            else
                round_shift = (v + ($signed({{(PW-1){1'b0}}, 1'b1}) <<< (sh - 1))) >>> sh;
        end
    endfunction

    // next-value temps (blocking, consumed by NBAs below)
    reg signed [PW-1:0] nxt_sum_re, nxt_sum_im;
    reg signed [PW-1:0] nxt_pr_re, nxt_pr_im;

    always @(posedge clk) begin
        if (rst) begin
            // FSM alignment preload (generator-derived constants);
            // delay-line contents intentionally NOT reset (PLAN.md 2.9 --
            // initialization invariance makes them irrelevant).
            ptr        <= {AW{1'b0}};
            phase_i    <= PRELOAD_I[AW-1:0];
            in_compute <= (PRELOAD_C != 0);
        end else if (ce) begin
            if (!in_compute) begin
                // PASS/FILL: emit stored product, write raw input
                out_re      <= d_re;
                out_im      <= d_im;
                mem_re[ptr] <= in_re;
                mem_im[ptr] <= in_im;
            end else begin
                // COMPUTE: pair input a (newer) with delayed d (older)
                nxt_sum_re = round_shift(w_in_re + w_d_re, SHIFT);
                nxt_sum_im = round_shift(w_in_im + w_d_im, SHIFT);
                out_re     <= nxt_sum_re[WIDTH-1:0];
                out_im     <= nxt_sum_im[WIDTH-1:0];

                // diff contract: OLDER - NEWER
                // NOTE: direct 4-product form; bit-identical to the
                // 3-product Karatsuba identity the DSP datapath will use
                // (exact integer arithmetic before the shared rounding
                // shift). Structural 3-DSP mapping lands in P5.
                nxt_pr_re = round_shift(
                    (w_d_re - w_in_re)*w_t_re
                        - (w_d_im - w_in_im)*w_t_im,
                    TD_PLUS_SHIFT);
                nxt_pr_im = round_shift(
                    (w_d_re - w_in_re)*w_t_im
                        + (w_d_im - w_in_im)*w_t_re,
                    TD_PLUS_SHIFT);
                mem_re[ptr] <= nxt_pr_re[WIDTH-1:0];
                mem_im[ptr] <= nxt_pr_im[WIDTH-1:0];
            end

            // FSM advance
            if (DEPTH > 1)
                ptr <= ptr + {{(AW-1){1'b0}}, 1'b1};
            phase_i <= phase_i + {{(AW-1){1'b0}}, 1'b1};
            if (phase_i == DEPTH_M1) begin
                phase_i    <= {AW{1'b0}};
                in_compute <= ~in_compute;
            end
        end
    end
endmodule

`default_nettype wire
