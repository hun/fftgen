// per-stage post-warm reset preloads are supplied by the generator as a
// macro in fft_preloads.vh (-G cannot carry >32-bit parameter values)
`ifdef FFTGEN_PRELOADS
`include "fft_preloads.vh"
`endif
`ifndef FFTGEN_PRELOAD_PACK
`define FFTGEN_PRELOAD_PACK 512'b0
`endif
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
    // 0 = DIF (native->bitreversed), 1 = DIT (bitreversed->native);
    // the twiddle table contents and stage geometry follow (see below)
    parameter integer TOPOLOGY       = 0,
    // twiddle table file: NUM_POINTS words, {re, im} signed MSB:LSB;
    // stage s occupies entries [BASE_s .. BASE_s + D_s - 1],
    // BASE_s = sum(D_t, t < s); D_s = N>>(s+1) (DIF) or 2^s (DIT)
    parameter TWIDDLE_FILE           = "fft_twiddles.mem",
    // internal growth headroom, generator-derived:
    // SAMPLE_WIDTH + max(0, num_stages - total_shift) + 1
    parameter integer INTERN_WIDTH   = SAMPLE_WIDTH + 5,
    // datapath pipeline layers per stage (golden model NLAYERS=5)
    parameter integer PIPE_DEPTH     = 5,
    // per-stage post-warm reset preloads, packed: for stage g (LSB first)
    //   {wptr(16), pwp(16), raddr(16), pipe(4), phase_i(8), compute(1)}
    // supplied by the generator via a macro (the -G parser caps at 32 bits)
    parameter [511:0] PRELOAD_PACK   = `FFTGEN_PRELOAD_PACK
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
    localparam integer LATENCY = N + PIPE_DEPTH * NSTAGES;
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
            // the final output register (m_re_r) adds one cycle, so the
            // valid latch fires at LATENCY-1 and is visible with the data
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
            localparam integer DEPTH     = (TOPOLOGY == 1) ? (1 << g)
                                                           : (N >> (g + 1));
            localparam integer SHIFT     = (SCALING_PACK >> (2*g)) & 3;
            // sum of delay depths of stages t < g (ROM base)
            localparam integer SUM_D     = (TOPOLOGY == 1) ? ((1 << g) - 1)
                                                           : (N - (N >> g));
            // FSM alignment preload (appendix A, pipelined):
            //   warm_s = -(SUM_D + PIPE_DEPTH*s) mod 2*D_s
            localparam integer WARM      =
                (((2*DEPTH) - ((SUM_D + PIPE_DEPTH*g) % (2*DEPTH))) % (2*DEPTH));
            localparam integer PRELOAD_I = WARM % DEPTH;
            localparam        PRELOAD_C = (WARM >= DEPTH) ? 1 : 0;
            // slice this stage's preload from the pack (61 bits each)
            localparam [511:0] PRE_SLICE = PRELOAD_PACK >> (61 * g);
            localparam [15:0] WPTR_PRE = PRE_SLICE[15:0];
            localparam [15:0] PWP_PRE  = PRE_SLICE[31:16];
            localparam [15:0] RADDR_PRE= PRE_SLICE[47:32];
            localparam [3:0]  PIPE_PRE = PRE_SLICE[51:48];
            localparam [7:0]  PRE_I    = PRE_SLICE[59:52];
            localparam        PRE_C    = PRE_SLICE[60];

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
                .PRELOAD_I      (PRE_I),
                .PRELOAD_C      (PRE_C),
                .WPTR_PRE       (WPTR_PRE),
                .PWP_PRE        (PWP_PRE),
                .RADDR_PRE      (RADDR_PRE),
                .PIPE_PRE       (PIPE_PRE),
                .TOPOLOGY       (TOPOLOGY)
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

// fftgen -- one radix-2 SDF stage, PIPELINED datapath (500 MHz target)
//
// Mirrors the golden model's K=5-layer pipeline (src/golden.py):
//   R2 read capture -> R3 butterfly -> R4 Karatsuba multiply
//   -> R5 combine -> R6 shift+out
// with two collision-free structures (PLAN.md 2.7):
//   - first-half delay RAM (2D slots, read lags write by D)
//   - product FIFO (2D slots, output D cycles after completion)
// The phase flag rides the pipeline (pipe_comp): COMPUTE cycles compute,
// PASS cycles pass the delayed-path values through.

`default_nettype none


module fft_stage #(
    parameter integer DEPTH          = 4,
    parameter integer WIDTH          = 16,   // internal sample width
    parameter integer SHIFT          = 1,    // per-stage scaling shift 0..2
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL= 17,
    parameter integer K_STRIDE       = 1,    // (unused; generator pre-permutes)
    parameter integer ROM_BASE       = 0,
    parameter integer NPTS           = 16,
    parameter [7:0]  PRELOAD_I      = 8'h0,  // FSM alignment preload
    parameter         PRELOAD_C      = 0,    // start in COMPUTE phase
    parameter [15:0]  WPTR_PRE       = 16'h0, // post-warm pointer state
    parameter [15:0]  PWP_PRE        = 16'h0,
    parameter [15:0]  RADDR_PRE      = 16'h0,
    parameter [3:0]   PIPE_PRE       = 4'h0,
    parameter integer TOPOLOGY       = 0     // 0 = DIF, 1 = DIT
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
    localparam RAMW = $clog2(2 * DEPTH);       // 2D slots
    localparam integer TD_PLUS_SHIFT = TWIDDLE_DECIMAL + SHIFT;
    localparam integer SHIFT_SUM = (TOPOLOGY == 1) ? TD_PLUS_SHIFT : SHIFT;
    localparam integer PW = WIDTH + TWIDDLE_WIDTH + 4;

    // first-half delay RAM (2D slots, sync read, read lags write by D)
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] ram_re [0:2*DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] ram_im [0:2*DEPTH-1];
    // product FIFO (2D slots, output D cycles after write)
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] pfifo_re [0:2*DEPTH-1];
    (* ram_style = "distributed" *)
    reg signed [WIDTH-1:0] pfifo_im [0:2*DEPTH-1];

    reg [RAMW-1:0] wptr /*verilator public_flat*/;                      // first-half write pointer
    reg [RAMW-1:0] pwp /*verilator public_flat*/;                       // product FIFO write pointer
    reg [RAMW-1:0] raddr_r /*verilator public_flat*/;                   // registered RAM read address
    wire [RAMW-1:0] pr = pwp - DEPTH[RAMW-1:0]; // product FIFO read ptr (lag D)

    // FSM
    reg          in_compute /*verilator public_flat*/;                  // 0 = PASS/FILL, 1 = COMPUTE
    reg [AW-1:0] phase_i /*verilator public_flat*/;                     // pair index within phase
    reg [3:0]    pipe_comp /*verilator public_flat*/;                   // phase flags riding the pipe

    // R2: read capture
    reg signed [WIDTH-1:0] d_reg_re /*verilator public_flat*/, d_reg_im;
    reg signed [WIDTH-1:0] a_reg_re /*verilator public_flat*/, a_reg_im;
    reg signed [TWIDDLE_WIDTH-1:0] tw_reg_re, tw_reg_im;
    // R3: butterfly
    reg signed [WIDTH-1:0] bfly_d_re /*verilator public_flat*/, bfly_d_im, bfly_s_re, bfly_s_im;
    reg signed [TWIDDLE_WIDTH-1:0] tw_d1_re, tw_d1_im;
    reg signed [WIDTH-1:0] d_dly_re, d_dly_im;
    reg signed [WIDTH-1:0] a_dly_re, a_dly_im;    // DIT
    // R4: multiply
    reg signed [PW-1:0] mreg_re /*verilator public_flat*/, mreg_im;
    reg signed [WIDTH-1:0] sum_dly_re, sum_dly_im;
    reg signed [WIDTH-1:0] d_dly2_re, d_dly2_im;
    // R5: combine
    reg signed [PW-1:0] comb_s_re /*verilator public_flat*/, comb_s_im;
    reg signed [PW-1:0] comb_p_re /*verilator public_flat*/, comb_p_im;
    // R6: output (out_re/out_im are the output ports)

    // twiddle decode + address (combinational, from the current pair index)
    wire signed [TWIDDLE_WIDTH-1:0] t_re = rom_data[TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
    wire signed [TWIDDLE_WIDTH-1:0] t_im = rom_data[TWIDDLE_WIDTH-1:0];
    wire [AW-1:0] pair_i = phase_i & (DEPTH[AW-1:0] - 1'b1);
    assign rom_addr = ROM_BASE[$clog2(NPTS)-1:0]
                      + {{($clog2(NPTS)-AW){1'b0}}, pair_i};

    // round-half-up arithmetic right shift
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

    // next-value temps
    reg signed [WIDTH-1:0] nxt_bf_d_re, nxt_bf_d_im, nxt_bf_s_re, nxt_bf_s_im;
    reg signed [PW-1:0] nxt_out_s, nxt_out_p;
    reg signed [PW-1:0] m1r, m2r, m3r;
    reg signed [PW-1:0] se_a_re, se_a_im, se_d_re, se_d_im, se_t_re, se_t_im;

    always @(posedge clk) begin
        if (rst) begin
            wptr        <= WPTR_PRE[RAMW-1:0];
            pwp         <= PWP_PRE[RAMW-1:0];
            raddr_r     <= RADDR_PRE[RAMW-1:0];
            pipe_comp   <= PIPE_PRE[3:0];
            in_compute  <= (PRELOAD_C != 0);
            phase_i     <= PRELOAD_I[AW-1:0];
            out_re      <= {WIDTH{1'b0}};
            out_im      <= {WIDTH{1'b0}};
        end else if (ce) begin
            // R6: shift + out; product FIFO write
            if (pipe_comp[3]) begin
                nxt_out_s = round_shift(comb_s_re, SHIFT_SUM);
                nxt_out_p = round_shift(comb_s_im, SHIFT_SUM);
                out_re <= nxt_out_s[WIDTH-1:0];
                out_im <= nxt_out_p[WIDTH-1:0];
                nxt_out_p = round_shift(comb_p_re, TD_PLUS_SHIFT);
                nxt_out_s = round_shift(comb_p_im, TD_PLUS_SHIFT);
                pfifo_re[pwp] <= nxt_out_p[WIDTH-1:0];
                pfifo_im[pwp] <= nxt_out_s[WIDTH-1:0];
            end else begin
                out_re <= pfifo_re[pr];
                out_im <= pfifo_im[pr];
            end

            // R5: combine (or passthrough)
            if (pipe_comp[2]) begin
                if (TOPOLOGY == 0) begin
                    comb_p_re <= mreg_re;
                    comb_p_im <= mreg_im;
                    comb_s_re <= {{(PW-WIDTH){sum_dly_re[WIDTH-1]}}, sum_dly_re};
                    comb_s_im <= {{(PW-WIDTH){sum_dly_im[WIDTH-1]}}, sum_dly_im};
                end else begin
                    // DIT: (d << td) +- t at 2^td scale
                    se_d_re = {{(PW-WIDTH){d_dly2_re[WIDTH-1]}}, d_dly2_re};
                    se_d_im = {{(PW-WIDTH){d_dly2_im[WIDTH-1]}}, d_dly2_im};
                    comb_s_re <= (se_d_re <<< TWIDDLE_DECIMAL) + mreg_re;
                    comb_s_im <= (se_d_im <<< TWIDDLE_DECIMAL) + mreg_im;
                    comb_p_re <= (se_d_re <<< TWIDDLE_DECIMAL) - mreg_re;
                    comb_p_im <= (se_d_im <<< TWIDDLE_DECIMAL) - mreg_im;
                end
            end else begin
                comb_s_re <= {{(PW-WIDTH){d_dly2_re[WIDTH-1]}}, d_dly2_re};
                comb_s_im <= {{(PW-WIDTH){d_dly2_im[WIDTH-1]}}, d_dly2_im};
                comb_p_re <= {{(PW-WIDTH){d_dly2_re[WIDTH-1]}}, d_dly2_re};
                comb_p_im <= {{(PW-WIDTH){d_dly2_im[WIDTH-1]}}, d_dly2_im};
            end

            // R4: Karatsuba multiply (or passthrough)
            if (pipe_comp[1]) begin
                if (TOPOLOGY == 0) begin
                    se_a_re = {{(PW-WIDTH){bfly_d_re[WIDTH-1]}}, bfly_d_re};
                    se_a_im = {{(PW-WIDTH){bfly_d_im[WIDTH-1]}}, bfly_d_im};
                end else begin
                    se_a_re = {{(PW-WIDTH){a_dly_re[WIDTH-1]}}, a_dly_re};
                    se_a_im = {{(PW-WIDTH){a_dly_im[WIDTH-1]}}, a_dly_im};
                end
                se_t_re = {{(PW-TWIDDLE_WIDTH){tw_d1_re[TWIDDLE_WIDTH-1]}}, tw_d1_re};
                se_t_im = {{(PW-TWIDDLE_WIDTH){tw_d1_im[TWIDDLE_WIDTH-1]}}, tw_d1_im};
                m1r = se_a_re * se_t_re;
                m2r = se_a_im * se_t_im;
                m3r = (se_a_re + se_a_im) * (se_t_re + se_t_im);
                mreg_re <= m1r - m2r;
                mreg_im <= m3r - m1r - m2r;
            end else begin
                mreg_re <= {{(PW-WIDTH){d_dly2_re[WIDTH-1]}}, d_dly2_re};
                mreg_im <= {{(PW-WIDTH){d_dly2_im[WIDTH-1]}}, d_dly2_im};
            end
            sum_dly_re <= bfly_s_re;
            sum_dly_im <= bfly_s_im;
            d_dly2_re  <= d_dly_re;
            d_dly2_im  <= d_dly_im;

            // R3: butterfly (or passthrough); twiddle/d/a delay regs.
            // NB: golden model lets a_dly/d_dly capture the NEWLY computed
            // butterfly values (their assignment follows the R3 update),
            // while sum_dly/d_dly2/tw_d1 hold the previous ones.
            if (pipe_comp[0]) begin
                if (TOPOLOGY == 0) begin
                    nxt_bf_d_re = d_reg_re - a_reg_re;
                    nxt_bf_d_im = d_reg_im - a_reg_im;
                    nxt_bf_s_re = d_reg_re + a_reg_re;
                    nxt_bf_s_im = d_reg_im + a_reg_im;
                end else begin
                    nxt_bf_d_re = d_reg_re;     // d rides to the combine
                    nxt_bf_d_im = d_reg_im;
                    nxt_bf_s_re = a_reg_re;     // a rides to the multiply
                    nxt_bf_s_im = a_reg_im;
                end
            end else begin
                nxt_bf_d_re = d_reg_re;
                nxt_bf_d_im = d_reg_im;
                nxt_bf_s_re = d_reg_re;
                nxt_bf_s_im = d_reg_im;
            end
            bfly_d_re <= nxt_bf_d_re;
            bfly_d_im <= nxt_bf_d_im;
            bfly_s_re <= nxt_bf_s_re;
            bfly_s_im <= nxt_bf_s_im;
            tw_d1_re <= tw_reg_re;
            tw_d1_im <= tw_reg_im;
            d_dly_re <= nxt_bf_d_re;   // golden: d_dly = new bfly_diff
            d_dly_im <= nxt_bf_d_im;
            if (TOPOLOGY == 1) begin
                a_dly_re <= nxt_bf_s_re;  // golden: a_dly = new bfly_sum
                a_dly_im <= nxt_bf_s_im;
            end

            // R2: first-half RAM write (PASS only) + read capture
            if (!in_compute) begin
                ram_re[wptr] <= in_re;          // PASS: first half
                ram_im[wptr] <= in_im;
            end
            d_reg_re <= ram_re[raddr_r];        // sync read (addr reg'd)
            d_reg_im <= ram_im[raddr_r];
            a_reg_re <= in_re;
            a_reg_im <= in_im;
            tw_reg_re <= t_re;
            tw_reg_im <= t_im;

            // FSM advance (subtract FULL depth: DEPTH[AW-1:0] would
            // truncate a power-of-two depth to zero)
            raddr_r <= (wptr + {{(RAMW-1){1'b0}}, 1'b1}
                        - DEPTH[RAMW-1:0]);     // next cycle's read addr
            wptr <= wptr + {{(RAMW-1){1'b0}}, 1'b1};
            pwp  <= pwp + {{(RAMW-1){1'b0}}, 1'b1};
            pipe_comp <= {pipe_comp[2:0], in_compute};
            phase_i <= phase_i + {{(AW-1){1'b0}}, 1'b1};
            if (phase_i == DEPTH[AW-1:0] - 1'b1) begin
                phase_i    <= {AW{1'b0}};
                in_compute <= ~in_compute;
            end
        end
    end
endmodule

`default_nettype wire
