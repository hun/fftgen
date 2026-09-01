// fftgen -- R=1 R2^3 SDF core (DIF, native->bitreversed).
// 3 merged radix-2^3 triples (G = N>>3, N>>6, N>>9 -- the smallest G is
// 16 for N=8192, safely above the small-G window-separation floor) +
// the remaining r2 stages as R2^2 pairs (D = N>>11, N>>13, ...).
//
// Chaining (validated in spikes/S7_r23/rtl_bringup/): stage j's phase
// counter runs (c - upstream_delay) mod period, where upstream_delay
// = sum(upstream GOLDEN latencies) + j*8 (per-stage H) + j (the
// inter-stage register handoff). For the r23 triples:
//   K_PRELOAD_j = -(sum_{i<j} (7*G_i+2) + 9*j) mod 8*G_j
// (H=8 per stage + 1 handoff = 9j total). The r22 leftovers use the
// r22's own convention, K_PRELOAD = -(upstream RTL latency) mod 4D,
// with the upstream RTL latency = sum(7G+10) over the triples +
// sum(3D+9) over the upstream r22 pairs.
//
// Same AXI-stream framing contract as fft_sdf_r22.v: tvalid/tuser/
// tlast ride at the fixed latency LATENCY = CORE_LAT + 1; the datapath
// and markers advance only while ce && s_axis_tvalid. INVERSE travels
// with the conjugate twiddle ROM (baked from one FFTConfig).

`default_nettype none

module fft_sdf_r23 #(
    parameter integer NUM_POINTS     = 8192,
    parameter integer SAMPLE_WIDTH   = 16,
    parameter integer SAMPLE_DECIMAL = 0,
    parameter integer OUTPUT_WIDTH   = 16,
    parameter integer OUTPUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    parameter integer SCALING_PACK   = 32'h01555555, // 1 shift/stage
    parameter integer INVERSE        = 0,
    parameter TWIDDLE_FILE_T0        = "fft_tw_r23_t0.mem",
    parameter TWIDDLE_FILE_T1        = "fft_tw_r23_t1.mem",
    parameter TWIDDLE_FILE_T2        = "fft_tw_r23_t2.mem",
    parameter TWIDDLE_FILE_L         = "fft_tw_r22_l.mem",
    // per-leftover-pair K_PRELOAD trim, empirically calibrated against
    // the golden: the r23->r22 handoff convention = one clock per
    // upstream r23 triple (verified =3 for NTRIP=3 at N=8192), so the
    // auto default (= NTRIP) should hold for every N
    parameter integer KP_L0_TRIM     = -1,   // -1 = auto (= NTRIP)
    parameter integer KP_L1_TRIM     = -1,
    parameter integer INTERN_WIDTH   = SAMPLE_WIDTH + 5
)(
    input  wire                      clk,
    input  wire                      ce,
    input  wire                      s_axis_tvalid,
    input  wire [SAMPLE_WIDTH-1:0]   s_axis_tdata_re,
    input  wire [SAMPLE_WIDTH-1:0]   s_axis_tdata_im,
    input  wire                      s_axis_tuser,
    input  wire                      s_axis_tlast,
    output wire                      m_axis_tvalid,
    output wire [OUTPUT_WIDTH-1:0]   m_axis_tdata_re,
    output wire [OUTPUT_WIDTH-1:0]   m_axis_tdata_im,
    output wire                      m_axis_tuser,
    output wire                      m_axis_tlast,
    input  wire                      rst
);

    // q8 = round(sqrt(2)/2 * 2^TD), computed as a constant function
    function integer q8_of;
        input integer td_;
        begin
            // 46341 = round(sqrt(2)/2 * 2^16).  Direct 46341*(1<<td)
            // overflows 32-bit Verilog integer arithmetic for td >= 16,
            // so shift instead of multiply.
            if (td_ >= 16)
                q8_of = 46341 << (td_ - 16);
            else
                q8_of = (46341 + (1 << (15 - td_))) >> (16 - td_);
        end
    endfunction

    localparam integer N       = NUM_POINTS;
    localparam integer NSTAGES = $clog2(N);   // 13 for N=8192
    // NTRIP: the largest triple count t in 1..3 with 3t <= NSTAGES,
    // (NSTAGES - 3t) even (the r22 leftovers come in pairs), and the
    // smallest triple G = N >> (3t) >= 8 (the small-G limit: a G <= 4
    // triple's ringB_p/q1 write->read lag goes negative).  G values are
    // powers of two, so >= 8 is the exact verified boundary.
    localparam integer NTRIP   = (NSTAGES >= 9 && (NSTAGES - 9) % 2 == 0
                                  && (N >> 9) >= 8) ? 3 :
                                 (NSTAGES >= 6 && (NSTAGES - 6) % 2 == 0
                                  && (N >> 6) >= 8) ? 2 :
                                 (NSTAGES >= 3 && (NSTAGES - 3) % 2 == 0
                                  && (N >> 3) >= 8) ? 1 : 0;
    localparam integer NR2    = NSTAGES - 3 * NTRIP;  // leftover r2 stages
    localparam integer NPAIRL = NR2 / 2;      // r22 leftover pairs
    initial if (NTRIP == 0)
        $error("fft_sdf_r23: no valid triple count for N=%0d (NSTAGES=%0d): parity or small-G limit (G>=8) fails -- use the r22 core", N, NSTAGES);
    initial if (NR2 % 2 != 0)
        $error("fft_sdf_r23: internal error: odd leftover count %0d", NR2);
    localparam integer TRIM0 = (KP_L0_TRIM < 0) ? NTRIP : KP_L0_TRIM;
    localparam integer TRIM1 = (KP_L1_TRIM < 0) ? NTRIP : KP_L1_TRIM;

    // r23 triple RTL latency = golden 7G+2 + H 8 + 1 emission reg
    // (the pfifo write precompute moved the emission one cycle later)
    function integer trip_rtl_lat;
        input integer m_;
        trip_rtl_lat = 7 * (N >> (3 * m_ + 3)) + 12;
    endfunction
    function integer trip_gold_lat;
        input integer m_;
        trip_gold_lat = 7 * (N >> (3 * m_ + 3)) + 2;
    endfunction
    // r22 pair RTL latency = 3D+9, D = N >> (3*NTRIP + 2*jj + 2)
    function integer lpair_D;
        input integer jj;
        lpair_D = (jj < NPAIRL) ? (N >> (3 * NTRIP + 2 * jj + 2)) : 0;
    endfunction
    function integer lpair_rtl_lat;
        input integer jj;
        lpair_rtl_lat = 3 * lpair_D(jj) + 9;
    endfunction
    function integer trip_lat_total;
        input integer nt;
        integer ii, acc;
        begin
            acc = 0;
            for (ii = 0; ii < nt; ii = ii + 1)
                acc = acc + trip_rtl_lat(ii);
            trip_lat_total = acc;
        end
    endfunction
    function integer lpair_lat_total;
        input integer np;
        integer ii, acc;
        begin
            acc = 0;
            for (ii = 0; ii < np; ii = ii + 1)
                acc = acc + lpair_rtl_lat(ii);
            lpair_lat_total = acc;
        end
    endfunction
    // the per-pair cumulative ROM base inside the single concatenated
    // leftover ROM: pair jj's 3*D slice sits at [base, base+3D)
    function integer pair_rom_base;
        input integer jj;
        integer ii, acc;
        begin
            acc = 0;
            for (ii = 0; ii < jj; ii = ii + 1)
                acc = acc + 3 * lpair_D(ii);
            pair_rom_base = acc;
        end
    endfunction
    localparam integer LROM_WORDS = pair_rom_base(NPAIRL);
    // r23 K_PRELOAD: -(sum_{i<j} golden lat + 10j) mod 8G_j
    // (H 8 + 2 output registers per upstream stage)
    function integer trip_kpre;
        input integer j_;
        integer ii, acc;
        begin
            acc = 0;
            for (ii = 0; ii < j_; ii = ii + 1)
                acc = acc + trip_gold_lat(ii) + 11;
            trip_kpre = ((8 * (N >> (3 * j_ + 3)))
                         - (acc % (8 * (N >> (3 * j_ + 3)))))
                        % (8 * (N >> (3 * j_ + 3)));
        end
    endfunction
    // r22 leftover pair K_PRELOAD: -(upstream RTL lat + trim) mod 4D.
    // The trim is the r23->r22 handoff convention = one clock per
    // upstream r23 triple (verified =3 for NTRIP=3; invariant to the
    // triple-latency bookkeeping shifts).
    function integer lpair_kpre;
        input integer jj;
        integer up, kk;
        begin
            up = 0;
            for (kk = 0; kk < NTRIP; kk = kk + 1)
                up = up + trip_rtl_lat(kk);
            for (kk = 0; kk < jj; kk = kk + 1)
                up = up + lpair_rtl_lat(kk);
            if (4 * lpair_D(jj) == 0)
                lpair_kpre = 0;   // unused pair slot (jj >= NPAIRL)
            else
                lpair_kpre = ((4 * lpair_D(jj))
                              - ((up + (jj ? TRIM1 : TRIM0))
                                 % (4 * lpair_D(jj))))
                             % (4 * lpair_D(jj));
        end
    endfunction

    localparam integer CORE_LAT = trip_lat_total(NTRIP);
    localparam integer LATENCY = CORE_LAT + lpair_lat_total(NPAIRL) + 1;
    localparam integer CNT_W   = $clog2(LATENCY + 1);

    wire run = ce && s_axis_tvalid;

    reg [CNT_W-1:0] cnt;
    reg             out_valid_r;
    always @(posedge clk) begin
        if (rst) begin
            cnt <= {CNT_W{1'b0}};
            out_valid_r <= 1'b0;
        end else if (run) begin
            if (cnt != LATENCY[CNT_W-1:0]) cnt <= cnt + 1'b1;
            if (cnt == LATENCY[CNT_W-1:0] - 1'b1) out_valid_r <= 1'b1;
        end
    end
    assign m_axis_tvalid = out_valid_r && run;

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

    // ---------------- stage chain ----------------
    wire signed [INTERN_WIDTH-1:0] in_x_re, in_x_im;
    assign in_x_re = $signed({{(INTERN_WIDTH-SAMPLE_WIDTH){s_axis_tdata_re[SAMPLE_WIDTH-1]}}, s_axis_tdata_re});
    assign in_x_im = $signed({{(INTERN_WIDTH-SAMPLE_WIDTH){s_axis_tdata_im[SAMPLE_WIDTH-1]}}, s_axis_tdata_im});

    wire signed [INTERN_WIDTH-1:0] t1_re, t1_im, t2_re, t2_im;
    wire signed [INTERN_WIDTH-1:0] l0_re, l0_im;

    // ---- triple 0 (always present) ----
    fft_stage_r23 #(
        .DEPTH          (N >> 3),
        .WIDTH          (INTERN_WIDTH),
        .SIGMA0         ((SCALING_PACK) & 3),
        .SIGMA1         ((SCALING_PACK >> 2) & 3),
        .SIGMA2         ((SCALING_PACK >> 4) & 3),
        .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
        .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
        .ROM_BASE       (0), .NPTS(8 * (N >> 3)),
        .INVERSE        (INVERSE),
        .Q8             (q8_of(TWIDDLE_DECIMAL)),
        .K_PRELOAD      (16'h0),
        .TWIDDLE_FILE   (TWIDDLE_FILE_T0)
    ) u_t0 ( .clk(clk), .ce(run), .rst(rst),
        .in_re(in_x_re), .in_im(in_x_im),
        .out_re(t1_re), .out_im(t1_im) );

    genvar tj;
    generate
        if (NTRIP >= 2) begin : has_t1
            localparam [15:0] KPRE = trip_kpre(1);
            fft_stage_r23 #(
                .DEPTH          (N >> 6),
                .WIDTH          (INTERN_WIDTH),
                .SIGMA0         ((SCALING_PACK >> 6) & 3),
                .SIGMA1         ((SCALING_PACK >> 8) & 3),
                .SIGMA2         ((SCALING_PACK >> 10) & 3),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       (0), .NPTS(8 * (N >> 6)),
                .INVERSE        (INVERSE),
                .Q8             (q8_of(TWIDDLE_DECIMAL)),
                .K_PRELOAD      (KPRE),
                .TWIDDLE_FILE   (TWIDDLE_FILE_T1)
            ) u_t1 ( .clk(clk), .ce(run), .rst(rst),
                .in_re(t1_re), .in_im(t1_im),
                .out_re(t2_re), .out_im(t2_im) );
        end else begin : no_t1
            assign t2_re = t1_re; assign t2_im = t1_im;
        end
        if (NTRIP >= 3) begin : has_t2
            localparam [15:0] KPRE = trip_kpre(2);
            fft_stage_r23 #(
                .DEPTH          (N >> 9),
                .WIDTH          (INTERN_WIDTH),
                .SIGMA0         ((SCALING_PACK >> 12) & 3),
                .SIGMA1         ((SCALING_PACK >> 14) & 3),
                .SIGMA2         ((SCALING_PACK >> 16) & 3),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       (0), .NPTS(8 * (N >> 9)),
                .INVERSE        (INVERSE),
                .Q8             (q8_of(TWIDDLE_DECIMAL)),
                .K_PRELOAD      (KPRE),
                .TWIDDLE_FILE   (TWIDDLE_FILE_T2)
            ) u_t2 ( .clk(clk), .ce(run), .rst(rst),
                .in_re(t2_re), .in_im(t2_im),
                .out_re(l0_re), .out_im(l0_im) );
        end else begin : no_t2
            assign l0_re = t2_re; assign l0_im = t2_im;
        end
    endgenerate

    // ---------------- r2 leftover pairs (NPAIRL-generated) ------------
    // One concatenated leftover ROM: pair jj's 3*D_jj twiddle slice sits
    // at ROM_BASE = sum_{i<jj} 3*D_i; every pair's ROM array spans the
    // full file (NPTS = LROM_WORDS) so one $readmemh serves all.
    wire signed [INTERN_WIDTH-1:0] chain_re [0:NPAIRL];
    wire signed [INTERN_WIDTH-1:0] chain_im [0:NPAIRL];
    assign chain_re[0] = l0_re;
    assign chain_im[0] = l0_im;
    genvar pj;
    generate
        for (pj = 0; pj < NPAIRL; pj = pj + 1) begin : lpairs
            localparam integer DD   = lpair_D(pj);
            localparam integer BASE = pair_rom_base(pj);
            localparam integer SG0  = (SCALING_PACK >> (2 * (3*NTRIP+2*pj))) & 3;
            localparam integer SG1  = (SCALING_PACK >> (2 * (3*NTRIP+2*pj+1))) & 3;
            localparam [15:0] KPRE  = lpair_kpre(pj);
            fft_stage_r22 #(
                .DEPTH          (DD),
                .WIDTH          (INTERN_WIDTH),
                .SIGMA0         (SG0), .SIGMA1(SG1),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       (BASE), .NPTS(LROM_WORDS),
                .INVERSE        (INVERSE),
                .K_PRELOAD      (KPRE),
                .TWIDDLE_FILE   (TWIDDLE_FILE_L)
            ) u_pair ( .clk(clk), .ce(run), .rst(rst),
                .in_re(chain_re[pj]), .in_im(chain_im[pj]),
                .out_re(chain_re[pj+1]), .out_im(chain_im[pj+1]) );
        end
    endgenerate
    wire signed [INTERN_WIDTH-1:0] core_w_re = chain_re[NPAIRL];
    wire signed [INTERN_WIDTH-1:0] core_w_im = chain_im[NPAIRL];
    // debug aliases (plain wires; iverilog does not resolve hierarchical
    // array-element refs reliably for NPAIRL != 4)
    wire signed [INTERN_WIDTH-1:0] dbg_p0 = chain_re[1];
    wire signed [INTERN_WIDTH-1:0] dbg_p0i = chain_im[1];
    wire signed [INTERN_WIDTH-1:0] dbg_p1 = (NPAIRL > 1) ? chain_re[2] : '0;
    wire signed [INTERN_WIDTH-1:0] dbg_p1i = (NPAIRL > 1) ? chain_im[2] : '0;
    wire signed [INTERN_WIDTH-1:0] dbg_p2 = (NPAIRL > 2) ? chain_re[3] : '0;
    wire signed [INTERN_WIDTH-1:0] dbg_p2i = (NPAIRL > 2) ? chain_im[3] : '0;
    wire signed [INTERN_WIDTH-1:0] dbg_p3 = (NPAIRL > 3) ? chain_re[4] : '0;
    wire signed [INTERN_WIDTH-1:0] dbg_p3i = (NPAIRL > 3) ? chain_im[4] : '0;

    wire signed [INTERN_WIDTH-1:0] core_re = core_w_re;
    wire signed [INTERN_WIDTH-1:0] core_im = core_w_im;

    // output quantize Q(sample_decimal) -> Q(output_decimal), saturated
    localparam integer RESHIFT = (SAMPLE_DECIMAL > OUTPUT_DECIMAL) ? SAMPLE_DECIMAL - OUTPUT_DECIMAL : 0;
    localparam integer QW = INTERN_WIDTH + RESHIFT + OUTPUT_WIDTH + 2;
    function [OUTPUT_WIDTH-1:0] quant_out;
        input signed [INTERN_WIDTH-1:0] v;
        reg signed [QW-1:0] t; reg signed [QW-1:0] hi, lo;
        reg [OUTPUT_WIDTH-1:0] ohi, olo;
        begin
            ohi = {{1'b0},{(OUTPUT_WIDTH-1){1'b1}}};
            olo = {{1'b1},{(OUTPUT_WIDTH-1){1'b0}}};
            hi={{(QW-OUTPUT_WIDTH){1'b0}},ohi};
            lo={{(QW-OUTPUT_WIDTH){1'b1}},olo};
            if (SAMPLE_DECIMAL>OUTPUT_DECIMAL) t=($signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}},v}) + ($signed({{(QW-1){1'b0}},1'b1})<<<(RESHIFT-1)))>>>RESHIFT;
            else if (SAMPLE_DECIMAL<OUTPUT_DECIMAL) t=$signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}},v})<<<(OUTPUT_DECIMAL-SAMPLE_DECIMAL);
            else t=$signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}},v});
            if (t>hi) quant_out=ohi; else if (t<lo) quant_out=olo; else quant_out=t[OUTPUT_WIDTH-1:0];
        end
    endfunction
    reg [OUTPUT_WIDTH-1:0] m_re_r, m_im_r;
    always @(posedge clk) if (run) begin m_re_r<=quant_out(core_re); m_im_r<=quant_out(core_im); end
    assign m_axis_tdata_re = m_re_r;
    assign m_axis_tdata_im = m_im_r;

endmodule
`default_nettype wire
