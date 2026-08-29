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
    // datapath pipeline layers per stage (golden model NLAYERS=10)
    parameter integer PIPE_DEPTH     = 10,
    // per-stage post-warm reset preloads, packed: for stage g (LSB first)
    //   {wptr(16), pwp(16), raddr(16), pipe(9), phase_i(16), compute(1)}
    // = 74 bits per stage (phase_i must cover DEPTH up to N/2 = 13 bits)
    // supplied by the generator via a macro (the -G parser caps at 32 bits)
    // width must cover the largest supported transform (74 bits/stage;
    // N=16384 -> 14 stages -> 1036 bits); narrower macros zero-extend
    parameter [4095:0] PRELOAD_PACK  = `FFTGEN_PRELOAD_PACK,
    // twiddle ROM implementation: "auto" applies the measured cutoff
    // (doc/mem_cutoffs.md S4), "distributed"/"block" force a style.
    // Block mode replicates the ROM per stage (BRAM has no multi-port
    // read) with the read fused into each stage's L0 capture register.
    parameter integer TWIDDLE_MEM   = 0       // 0=auto 1=distributed 2=block
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
    // twiddle ROM style: "auto" moves the table to block RAM once one
    // RAMB36 per stage is cheaper than the distributed LUTs (measured
    // crossover in doc/mem_cutoffs.md S4). Block mode replicates the ROM
    // inside every stage (one sync read port each); distributed mode
    // keeps ONE shared async-read LUTROM for all stages.
    // ------------------------------------------------------------------
    localparam integer TW_ROM_BITS = N * TWIDDLE_WIDTH * 2;
    // resolved style passed to the stages: 1 = distributed, 2 = block
    localparam integer TW_AUTO = (TW_ROM_BITS >= 8192) ? 2 : 1;
    localparam integer TW_MODE_EFF = (TWIDDLE_MEM == 0) ? TW_AUTO
                                                        : TWIDDLE_MEM;

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
            // trivial-twiddle stage: the DIF LAST TWO stages and the DIT
            // FIRST TWO stages multiply only by single-component twiddles
            // (W^0 real, W^{N/4} = +/-j -- the k = 0 and k = N/2^(s+2)
            // pairs of the DIF schedule). The product path is exact fabric
            // shift/subtract logic -- no DSPs.
            localparam integer TRIVIAL   = (TOPOLOGY == 1) ? (g <= 1)
                                                           : (g >= NSTAGES - 2);
            // sum of delay depths of stages t < g (ROM base)
            localparam integer SUM_D     = (TOPOLOGY == 1) ? ((1 << g) - 1)
                                                           : (N - (N >> g));
            // FSM alignment preload (appendix A, pipelined):
            //   warm_s = -(SUM_D + PIPE_DEPTH*s) mod 2*D_s
            localparam integer WARM      =
                (((2*DEPTH) - ((SUM_D + PIPE_DEPTH*g) % (2*DEPTH))) % (2*DEPTH));
            localparam integer PRELOAD_I = WARM % DEPTH;
            localparam        PRELOAD_C = (WARM >= DEPTH) ? 1 : 0;
            // slice this stage's preload from the pack (74 bits each)
            localparam [4159:0] PRE_SLICE =
                {{64{1'b0}}, PRELOAD_PACK} >> (74 * g);
            localparam [15:0] WPTR_PRE = PRE_SLICE[15:0];
            localparam [15:0] PWP_PRE  = PRE_SLICE[31:16];
            localparam [15:0] RADDR_PRE= PRE_SLICE[47:32];
            localparam [8:0]  PIPE_PRE = PRE_SLICE[56:48];
            localparam [15:0] PRE_I   = PRE_SLICE[72:57];
            localparam        PRE_C    = PRE_SLICE[73];

            fft_stage #(
                .DEPTH          (DEPTH),
                .WIDTH          (INTERN_WIDTH),
                .SHIFT          (SHIFT),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       (SUM_D),
                .NPTS           (N),
                .PRELOAD_I      (PRE_I),
                .PRELOAD_C      (PRE_C),
                .WPTR_PRE       (WPTR_PRE),
                .PWP_PRE        (PWP_PRE),
                .RADDR_PRE      (RADDR_PRE),
                .PIPE_PRE       (PIPE_PRE),
                .TOPOLOGY       (TOPOLOGY),
                .TRIVIAL        (TRIVIAL),
                .TWIDDLE_MEM    (TW_MODE_EFF),
                .TWIDDLE_FILE   (TWIDDLE_FILE)
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
                .out_im   (st_out_im[g])
            );

        end
    endgenerate

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
    parameter integer ROM_BASE       = 0,
    parameter integer NPTS           = 16,
    // NO declared width: the parameter takes the width of the value
    // passed by the generator (phase must cover DEPTH up to N/2;
    // N=16384 needs 13 bits). An explicit narrow range here was the
    // root cause of the N>=2048 failures (505 & 0xFF = 249).
    parameter        PRELOAD_I      = 0,    // FSM alignment preload
    parameter         PRELOAD_C      = 0,    // start in COMPUTE phase
    parameter [15:0]  WPTR_PRE       = 16'h0, // post-warm pointer state
    parameter [15:0]  PWP_PRE        = 16'h0,
    parameter [15:0]  RADDR_PRE      = 16'h0,
    parameter [8:0]   PIPE_PRE       = 9'h0,
    parameter integer TOPOLOGY       = 0,    // 0 = DIF, 1 = DIT
    parameter integer K_STRIDE       = 1,    // (unused; generator pre-permutes twiddles)
    // 1 = trivial-twiddle stage (W^0 = (1,0) only): the product path is
    // an exact pass-through -- no DSPs. DIF stage n-1 / DIT stage 0.
    parameter integer TRIVIAL        = 0,
    // block-mode twiddle ROM: local per-stage replica, read fused into
    // the L0 capture (t_reg doubles as the BRAM output register)
    parameter integer TWIDDLE_MEM    = 0,    // 0=auto 1=distributed 2=block
    parameter         TWIDDLE_FILE   = "fft_twiddles.mem"
)(
    input  wire             clk,
    input  wire             ce,
    input  wire             rst,
    input  wire signed [WIDTH-1:0] in_re,
    input  wire signed [WIDTH-1:0] in_im,
    output reg  signed [WIDTH-1:0] out_re,
    output reg  signed [WIDTH-1:0] out_im
);

    localparam AW = (DEPTH > 1) ? $clog2(DEPTH) : 1;
    localparam RAMW = $clog2(2 * DEPTH);       // 2D slots
    localparam integer TD_PLUS_SHIFT = TWIDDLE_DECIMAL + SHIFT;
    localparam integer SHIFT_SUM = (TOPOLOGY == 1) ? TD_PLUS_SHIFT : SHIFT;
    // W^0 quantizes to (2^td - 1, 0) (the Q-format encoding of +1.0,
    // saturated -- see twiddles.py _mag_table). The trivial-stage product
    // path must therefore compute pr * (2^td - 1) = (pr << td) - pr
    // EXACTLY (a fabric shift+subtract, no DSP); the -pr correction only
    // applies when 2^td saturates (td == width-1, the default).
    localparam integer TRIVIAL_SUB = (TWIDDLE_DECIMAL >= TWIDDLE_WIDTH - 1)
                                     ? 1 : 0;
    localparam integer PW = WIDTH + TWIDDLE_WIDTH + 4;
    // native complex-multiply accumulate width: 17b data x 18b twiddle
    // products (35b) plus one guard bit for the +/- accumulation

    // ------------------------------------------------------------------
    // memory style policy (decided cutoffs, see doc/mem_cutoffs.md S3):
    //   array bits <= 1024      -> distributed (LUTRAM; a RAMB36 would
    //                              sit >97% empty)
    //   array bits <  262144    -> block (RAMB36/E2; up to 8 tiles, still
    //                              cheaper than one URAM288)
    //   else                    -> ultra (URAM288)
    // All three shapes are SDP-compatible by construction: sync reads
    // through d_reg/out_reg, read address structurally disjoint from the
    // write address (lag D >= 1). The output register doubles as the RAM
    // output register so BRAM clock-to-out stays off the critical path.
    // ------------------------------------------------------------------
    localparam integer MEM_BITS = 2 * DEPTH * WIDTH;
    localparam integer MEM_STYLE = (MEM_BITS <= 1024) ? 0 :
                                   (MEM_BITS < 262144) ? 1 : 2;
    generate
        if (MEM_STYLE == 0) begin : g_mem
            (* ram_style = "distributed" *)
            reg signed [WIDTH-1:0] ram_re [0:2*DEPTH-1];
            (* ram_style = "distributed" *)
            reg signed [WIDTH-1:0] ram_im [0:2*DEPTH-1];
            (* ram_style = "distributed" *)
            reg signed [WIDTH-1:0] pfifo_re [0:2*DEPTH-1];
            (* ram_style = "distributed" *)
            reg signed [WIDTH-1:0] pfifo_im [0:2*DEPTH-1];
        end else if (MEM_STYLE == 1) begin : g_mem
            (* ram_style = "block" *)
            reg signed [WIDTH-1:0] ram_re [0:2*DEPTH-1];
            (* ram_style = "block" *)
            reg signed [WIDTH-1:0] ram_im [0:2*DEPTH-1];
            (* ram_style = "block" *)
            reg signed [WIDTH-1:0] pfifo_re [0:2*DEPTH-1];
            (* ram_style = "block" *)
            reg signed [WIDTH-1:0] pfifo_im [0:2*DEPTH-1];
        end else begin : g_mem
            // ultra: only reached at N*WIDTH >= 262144 (N >= 16384 at
            // 16-bit). Geometry unverified (doc/mem_cutoffs.md proves a
            // fixed 4Kx72 shape); widths other than 18/36/72 need
            // explicit packing or Vivado may fall back to BRAM.
            (* ram_style = "ultra" *)
            reg signed [WIDTH-1:0] ram_re [0:2*DEPTH-1];
            (* ram_style = "ultra" *)
            reg signed [WIDTH-1:0] ram_im [0:2*DEPTH-1];
            (* ram_style = "ultra" *)
            reg signed [WIDTH-1:0] pfifo_re [0:2*DEPTH-1];
            (* ram_style = "ultra" *)
            reg signed [WIDTH-1:0] pfifo_im [0:2*DEPTH-1];
        end
    endgenerate

    reg [RAMW-1:0] wptr /*verilator public_flat*/;                      // first-half write pointer
    reg [RAMW-1:0] pwp /*verilator public_flat*/;                       // product FIFO write pointer
    reg [RAMW-1:0] raddr_r /*verilator public_flat*/;                   // registered RAM read address
    // registered product-FIFO read address. Tracks pwp - D via the
    // +1 (next-cycle) form so the read timing is identical to a direct
    // combinational pr = pwp - D, but the address is registered for the
    // BRAM/LUTRAM address input (breaks the pointer->memory->out path).
    reg [RAMW-1:0] pr_r /*verilator public_flat*/;

    // FSM
    reg          in_compute /*verilator public_flat*/;                  // 0 = PASS/FILL, 1 = COMPUTE
    reg [AW-1:0] phase_i /*verilator public_flat*/;                     // pair index within phase
    reg [8:0]    pipe_comp /*verilator public_flat*/;                   // phase flags riding the pipe

    // ------------------------------------------------------------------
    // 10-layer datapath (mirrors golden _SDFStage step(), NLAYERS=10):
    //   L0 d_bram/a_reg/t_reg      BRAM output register + input + twiddle
    //   L1 d1/a1/t1                first DSP input register (A/D, B)
    //   L2 d2/a2/t2                second DSP input register (A/D, B)
    //   L3 bfly_d/bfly_s/t3        pre-adder -> ADREG (diff), sum in fabric
    //   L4 prod2/prod4 + freeze    im-path products (MREG) + re operands
    //   L5 c1/c2 + prod1/prod3     C-port regs (CREG) + re-path products
    //   L6 p                       post-adder (ALU P -/+ C -> PREG)
    //   L7 comb_s/comb_p           fabric combine (aligns sum/product)
    //   L8 shift_s/shift_p         round-half-up shift staging (ungated)
    //   L9 out_re/out_im           output register + product-FIFO write
    // Butterfly pre-adder width: d-a/d+a need one bit more than the
    // stage datapath (WIDTH+1). Everything from the pre-adder through
    // the products rides at BW; the >>SHIFT at L8 brings values back.
    localparam integer BW = WIDTH + 1;
    localparam integer MWB = BW + TWIDDLE_WIDTH + 1;

    // L0: BRAM output register + input + twiddle
    reg signed [WIDTH-1:0] d_bram_re /*verilator public_flat*/, d_bram_im;
    reg signed [WIDTH-1:0] a_reg_re /*verilator public_flat*/, a_reg_im;
    reg signed [TWIDDLE_WIDTH-1:0] t_reg_re, t_reg_im;
    // L1: first DSP input register
    reg signed [WIDTH-1:0] d1_re, d1_im, a1_re, a1_im;
    reg signed [TWIDDLE_WIDTH-1:0] t1_re, t1_im;
    // L2: second DSP input register
    reg signed [WIDTH-1:0] d2_re, d2_im, a2_re, a2_im;
    reg signed [TWIDDLE_WIDTH-1:0] t2_re, t2_im;
    // L3: butterfly (pre-adder -> ADREG) + twiddle third hop
    reg signed [BW-1:0] bfly_d_re /*verilator public_flat*/, bfly_d_im;
    reg signed [BW-1:0] bfly_s_re /*verilator public_flat*/, bfly_s_im;
    reg signed [TWIDDLE_WIDTH-1:0] t3_re, t3_im;
    // L4: im-path products (MREG) + frozen re-path operands
    reg signed [MWB-1:0] prod2, prod4;
    reg signed [BW-1:0] bfly_h_re, bfly_h_im;
    reg signed [TWIDDLE_WIDTH-1:0] t3h_re, t3h_im;
    // L5: C-port registers (CREG) + re-path products
    reg signed [MWB-1:0] c1, c2;
    reg signed [MWB-1:0] prod1, prod3;
    // L6: post-adder (PREG)
    reg signed [MWB-1:0] p_re /*verilator public_flat*/, p_im;
    // sum-path delay chain (matches the product path depth)
    reg signed [BW-1:0] s1_re, s1_im, s2_re, s2_im, s3_re, s3_im;
    // L7: combine
    reg signed [PW-1:0] comb_s_re /*verilator public_flat*/, comb_s_im;
    reg signed [PW-1:0] comb_p_re /*verilator public_flat*/, comb_p_im;
    // L8: shift staging
    reg signed [PW-1:0] shift_s_re, shift_s_im, shift_p_re, shift_p_im;
    // L9: output (out_re/out_im are the output ports)

    // Twiddle ROM, one replica per stage (Vivado already replicates a
    // multi-reader distributed ROM, so per-stage copies are physically
    // equivalent for LUTRAM and are what makes block mode possible --
    // a RAMB36 has one sync read port). Two style variants are declared;
    // the unused one is unreferenced and pruned by synthesis.
    // The read is fused into the main always block's L0 capture so the
    // capture register maps onto the BRAM output register: identical
    // cycle behavior to an async-read array captured at the same edge.
    (* ram_style = "distributed" *)
    reg signed [TWIDDLE_WIDTH*2-1:0] tw_rom_d [0:NPTS-1];
    (* ram_style = "block" *)
    reg signed [TWIDDLE_WIDTH*2-1:0] tw_rom_b [0:NPTS-1];

    initial begin
        $readmemh(TWIDDLE_FILE, tw_rom_d);
        $readmemh(TWIDDLE_FILE, tw_rom_b);
    end

    // multiplier operand source: DIF multiplies the butterfly diff
    // (pre-adder output), DIT rides 'a' to the multiply
    wire signed [BW-1:0] pr_re = (TOPOLOGY == 0) ? bfly_d_re : bfly_s_re;
    wire signed [BW-1:0] pr_im = (TOPOLOGY == 0) ? bfly_d_im : bfly_s_im;

    // single-component twiddle product (TRIVIAL stages only):
    //   f(x, w) = x * w,  w in {0, +/-c},  c = 2^td (saturated to
    //   2^td - 1 when td == width-1, the default -- twiddles.py sat)
    // computed as exact fabric logic:  x*c = (x << td) - x, then a
    // zero/sign select -- NO DSP. Value-identical to the golden's
    // exact cmul products (the full stage computes the same value on
    // DSP48s; here the twiddle's zero component simply forces 0).
    function signed [MWB-1:0] trivial_prod;
        input signed [BW-1:0] x;
        input signed [TWIDDLE_WIDTH-1:0] w;
        reg signed [MWB-1:0] se_x;
        reg signed [MWB-1:0] xc;
        begin
            se_x = {{(MWB-BW){x[BW-1]}}, x};
            xc = (se_x <<< TWIDDLE_DECIMAL)
                 - (TRIVIAL_SUB ? se_x : {MWB{1'b0}});
            trivial_prod = (w == {TWIDDLE_WIDTH{1'b0}}) ? {MWB{1'b0}}
                          : (w[TWIDDLE_WIDTH-1] ? -xc : xc);
        end
    endfunction

    // twiddle decode + address (combinational, from the current pair index)
    // twiddle read address (stage-local ROM): base + pair index
    wire [$clog2(NPTS)-1:0] rom_addr;
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
    // WIDTH->BW sign extensions for the pre-adder operands (the add
    // itself must be evaluated at BW or the carry out of bit WIDTH-1
    // is lost before the scaling shift ever sees it)
    wire signed [BW-1:0] d2_ext_re = {{(BW-WIDTH){d2_re[WIDTH-1]}}, d2_re};
    wire signed [BW-1:0] d2_ext_im = {{(BW-WIDTH){d2_im[WIDTH-1]}}, d2_im};
    wire signed [BW-1:0] a2_ext_re = {{(BW-WIDTH){a2_re[WIDTH-1]}}, a2_re};
    wire signed [BW-1:0] a2_ext_im = {{(BW-WIDTH){a2_im[WIDTH-1]}}, a2_im};
    reg signed [PW-1:0] se_d_re, se_d_im;

    always @(posedge clk) begin
        if (rst) begin
            wptr        <= WPTR_PRE[RAMW-1:0];
            pwp         <= PWP_PRE[RAMW-1:0];
            raddr_r     <= RADDR_PRE[RAMW-1:0];
            pr_r        <= PWP_PRE[RAMW-1:0] - DEPTH[RAMW-1:0];
            pipe_comp   <= PIPE_PRE[8:0];
            in_compute  <= (PRELOAD_C != 0);
            phase_i     <= PRELOAD_I[AW-1:0];
            out_re      <= {WIDTH{1'b0}};
            out_im      <= {WIDTH{1'b0}};
        end else if (ce) begin
            // L9: output register + product-FIFO write-back. Write and
            // read share this layer (COMPUTE writes the rounded product
            // at pwp; PASS reads the product written D cycles earlier at
            // pr = pwp - D), so the read/write windows align.
            if (pipe_comp[8]) begin
                g_mem.pfifo_re[pwp] <= shift_p_re[WIDTH-1:0];
                g_mem.pfifo_im[pwp] <= shift_p_im[WIDTH-1:0];
                out_re <= shift_s_re[WIDTH-1:0];
                out_im <= shift_s_im[WIDTH-1:0];
            end else begin
                out_re <= g_mem.pfifo_re[pr_r];
                out_im <= g_mem.pfifo_im[pr_r];
            end

            // L8: round + shift staging (ungated; only consumed by L9)
            shift_s_re <= round_shift(comb_s_re, SHIFT_SUM);
            shift_s_im <= round_shift(comb_s_im, SHIFT_SUM);
            shift_p_re <= round_shift(comb_p_re, TD_PLUS_SHIFT);
            shift_p_im <= round_shift(comb_p_im, TD_PLUS_SHIFT);

            // L7: combine (or passthrough) -- aligns the sum path
            // (delayed butterfly sum / DIT d) with the product path (PREG)
            if (pipe_comp[6]) begin
                if (TOPOLOGY == 0) begin
                    comb_p_re <= {{(PW-MWB){p_re[MWB-1]}}, p_re};
                    comb_p_im <= {{(PW-MWB){p_im[MWB-1]}}, p_im};
                    comb_s_re <= {{(PW-BW){s3_re[BW-1]}}, s3_re};
                    comb_s_im <= {{(PW-BW){s3_im[BW-1]}}, s3_im};
                end else begin
                    // DIT: (d << td) +- t at 2^td scale
                    se_d_re = {{(PW-BW){s3_re[BW-1]}}, s3_re};
                    se_d_im = {{(PW-BW){s3_im[BW-1]}}, s3_im};
                    comb_s_re <= (se_d_re <<< TWIDDLE_DECIMAL)
                                 + {{(PW-MWB){p_re[MWB-1]}}, p_re};
                    comb_s_im <= (se_d_im <<< TWIDDLE_DECIMAL)
                                 + {{(PW-MWB){p_im[MWB-1]}}, p_im};
                    comb_p_re <= (se_d_re <<< TWIDDLE_DECIMAL)
                                 - {{(PW-MWB){p_re[MWB-1]}}, p_re};
                    comb_p_im <= (se_d_im <<< TWIDDLE_DECIMAL)
                                 - {{(PW-MWB){p_im[MWB-1]}}, p_im};
                end
            end else begin
                comb_s_re <= {{(PW-BW){s3_re[BW-1]}}, s3_re};
                comb_s_im <= {{(PW-BW){s3_im[BW-1]}}, s3_im};
                comb_p_re <= {{(PW-BW){s3_re[BW-1]}}, s3_re};
                comb_p_im <= {{(PW-BW){s3_im[BW-1]}}, s3_im};
            end

            // L6: post-adder (PREG): re = prod1 - c1, im = prod3 + c2.
            // The im-path products ran one cycle ahead (L4); their MREGs
            // route to the re-path DSPs' C ports (CREG at L5), so the
            // ALU sees same-pair P (prod1/prod3, L5) and C (c1/c2, L5).
            if (pipe_comp[5]) begin
                p_re <= prod1 - c1;
                p_im <= prod3 + c2;
            end else begin
                p_re <= {MWB{1'b0}};
                p_im <= {MWB{1'b0}};
            end
            s3_re <= s2_re;
            s3_im <= s2_im;

            // L5: C-port registers (CREG) + re-path products (one cycle
            // behind the im-path, from the frozen L4 operands). TRIVIAL:
            // single-component twiddles (W^0 / +/-j): exact fabric
            // shift/subtract products, no DSP.
            if (pipe_comp[4]) begin
                if (TRIVIAL) begin
                    c1 <= prod2;
                    c2 <= prod4;
                    prod1 <= trivial_prod(bfly_h_re, t3h_re);
                    prod3 <= trivial_prod(bfly_h_re, t3h_im);
                end else begin
                    c1 <= prod2;
                    c2 <= prod4;
                    prod1 <= bfly_h_re * t3h_re;
                    prod3 <= bfly_h_re * t3h_im;
                end
            end else begin
                c1 <= {MWB{1'b0}};
                c2 <= {MWB{1'b0}};
                prod1 <= {MWB{1'b0}};
                prod3 <= {MWB{1'b0}};
            end
            s2_re <= s1_re;
            s2_im <= s1_im;

            // L4: im-path products (MREG) + freeze of the re-path
            // operands (the re multiply runs one cycle later so the DSP
            // C-port pairing P - C sees the same pair). TRIVIAL stages
            // multiply by single-component twiddles (W^0 / +/-j):
            // prod2 = im*ti, prod4 = im*tr -- exact fabric
            // shift/subtract logic, no DSP (value-identical to the
            // golden's exact cmul(pr, tw[k])).
            if (pipe_comp[3]) begin
                if (TRIVIAL) begin
                    prod2 <= trivial_prod(pr_im, t3_im);
                    prod4 <= trivial_prod(pr_im, t3_re);
                end else begin
                    prod2 <= pr_im * t3_im;
                    prod4 <= pr_im * t3_re;
                end
                bfly_h_re <= pr_re;
                bfly_h_im <= pr_im;
                t3h_re <= t3_re;
                t3h_im <= t3_im;
            end else begin
                prod2 <= {MWB{1'b0}};
                prod4 <= {MWB{1'b0}};
                bfly_h_re <= {BW{1'b0}};
                bfly_h_im <= {BW{1'b0}};
                t3h_re <= {TWIDDLE_WIDTH{1'b0}};
                t3h_im <= {TWIDDLE_WIDTH{1'b0}};
            end
            // sum path: DIF carries the butterfly sum; DIT carries d
            s1_re <= (TOPOLOGY == 0) ? bfly_s_re : bfly_d_re;
            s1_im <= (TOPOLOGY == 0) ? bfly_s_im : bfly_d_im;

            // L3: butterfly (pre-adder -> ADREG) + twiddle third hop.
            // UNGATED: the PASS-phase values are never consumed (the
            // products and combine are phase-gated downstream), and an
            // unconditional D-A expression lets Vivado map the diff into
            // the DSP48E2 pre-adder (ADREG) instead of fabric carries.
            if (TOPOLOGY == 0) begin
                bfly_d_re <= d2_ext_re - a2_ext_re;
                bfly_d_im <= d2_ext_im - a2_ext_im;
                bfly_s_re <= d2_ext_re + a2_ext_re;
                bfly_s_im <= d2_ext_im + a2_ext_im;
            end else begin
                bfly_d_re <= d2_ext_re;        // d rides to the combine
                bfly_d_im <= d2_ext_im;
                bfly_s_re <= a2_ext_re;        // a rides to the multiply
                bfly_s_im <= a2_ext_im;
            end
            t3_re <= t2_re;
            t3_im <= t2_im;

            // L2: second DSP input register (passthrough)
            d2_re <= d1_re;
            d2_im <= d1_im;
            a2_re <= a1_re;
            a2_im <= a1_im;
            t2_re <= t1_re;
            t2_im <= t1_im;

            // L1: first DSP input register (passthrough)
            d1_re <= d_bram_re;
            d1_im <= d_bram_im;
            a1_re <= a_reg_re;
            a1_im <= a_reg_im;
            t1_re <= t_reg_re;
            t1_im <= t_reg_im;

            // L0: capture -- first-half RAM write (PASS only) + BRAM
            // output register (registered read) + input + twiddle
            if (!in_compute) begin
                g_mem.ram_re[wptr] <= in_re;
                g_mem.ram_im[wptr] <= in_im;
            end
            d_bram_re <= g_mem.ram_re[raddr_r];      // BRAM output register
            d_bram_im <= g_mem.ram_im[raddr_r];
            a_reg_re <= in_re;
            a_reg_im <= in_im;
            if (TWIDDLE_MEM == 2) begin
                // block: sync read straight into the capture register --
                // t_reg IS the BRAM output register
                t_reg_re <= tw_rom_b[rom_addr][TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
                t_reg_im <= tw_rom_b[rom_addr][TWIDDLE_WIDTH-1:0];
            end else begin
                // distributed: async read captured at the same edge as
                // before (identical cycle behavior)
                t_reg_re <= tw_rom_d[rom_addr][TWIDDLE_WIDTH*2-1:TWIDDLE_WIDTH];
                t_reg_im <= tw_rom_d[rom_addr][TWIDDLE_WIDTH-1:0];
            end

            // FSM advance (subtract FULL depth: DEPTH[AW-1:0] would
            // truncate a power-of-two depth to zero)
            raddr_r <= (wptr + {{(RAMW-1){1'b0}}, 1'b1}
                        - DEPTH[RAMW-1:0]);     // next cycle's read addr
            pr_r    <= (pwp + {{(RAMW-1){1'b0}}, 1'b1}
                        - DEPTH[RAMW-1:0]);     // next cycle's pfifo read addr
            wptr <= wptr + {{(RAMW-1){1'b0}}, 1'b1};
            pwp  <= pwp + {{(RAMW-1){1'b0}}, 1'b1};
            pipe_comp <= {pipe_comp[7:0], in_compute};
            phase_i <= phase_i + {{(AW-1){1'b0}}, 1'b1};
            if (phase_i == DEPTH[AW-1:0] - 1'b1) begin
                phase_i    <= {AW{1'b0}};
                in_compute <= ~in_compute;
            end
        end
    end

endmodule

`default_nettype wire
