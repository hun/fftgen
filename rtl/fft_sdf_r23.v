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
    parameter TWIDDLE_FILE_L0        = "fft_tw_r22_l0.mem",
    parameter TWIDDLE_FILE_L1        = "fft_tw_r22_l1.mem",
    // per-leftover-pair K_PRELOAD trim, empirically calibrated against
    // the golden (scan_l0.py + bringup_core.py: both pairs need +3 for
    // N=8192, i.e. one clock per upstream r23 triple -- the r23->r22
    // handoff convention differs from the r23->r23 one)
    parameter integer KP_L0_TRIM     = 3,
    parameter integer KP_L1_TRIM     = 3,
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
    localparam integer NTRIP   = 3;           // r23 triples (stages 0..8)
    localparam integer NR2    = NSTAGES - 3 * NTRIP;  // leftover r2 stages
    localparam integer NPAIRL = NR2 / 2;      // r22 leftover pairs
    initial if (NR2 % 2 != 0)
        $error("fft_sdf_r23: NSTAGES-3*3 must be even (got %0d r2 leftovers)", NR2);

    // r23 triple RTL latency = golden 7G+2 + H 8 + 1 emission reg
    // (the pfifo write precompute moved the emission one cycle later)
    function integer trip_rtl_lat;
        input integer m_;
        trip_rtl_lat = 7 * (N >> (3 * m_ + 3)) + 11;
    endfunction
    function integer trip_gold_lat;
        input integer m_;
        trip_gold_lat = 7 * (N >> (3 * m_ + 3)) + 2;
    endfunction
    // r22 pair RTL latency = 3D+9, D = N >> (3*NTRIP + 2*jj + 2)
    function integer lpair_D;
        input integer jj;
        lpair_D = N >> (3 * NTRIP + 2 * jj + 2);
    endfunction
    function integer lpair_rtl_lat;
        input integer jj;
        lpair_rtl_lat = 3 * lpair_D(jj) + 9;
    endfunction
    // r23 K_PRELOAD: -(sum_{i<j} golden lat + 10j) mod 8G_j
    // (H 8 + 2 output registers per upstream stage)
    function integer trip_kpre;
        input integer j_;
        integer ii, acc;
        begin
            acc = 0;
            for (ii = 0; ii < j_; ii = ii + 1)
                acc = acc + trip_gold_lat(ii) + 10;
            trip_kpre = ((8 * (N >> (3 * j_ + 3)))
                         - (acc % (8 * (N >> (3 * j_ + 3)))))
                        % (8 * (N >> (3 * j_ + 3)));
        end
    endfunction
    // r22 leftover pair K_PRELOAD: -(upstream RTL lat + trim) mod 4D
    function integer lpair_kpre;
        input integer jj;
        integer up, kk;
        begin
            up = 0;
            for (kk = 0; kk < NTRIP; kk = kk + 1)
                up = up + trip_rtl_lat(kk);
            for (kk = 0; kk < jj; kk = kk + 1)
                up = up + lpair_rtl_lat(kk);
            lpair_kpre = ((4 * lpair_D(jj))
                          - ((up + (jj ? KP_L1_TRIM : KP_L0_TRIM))
                             % (4 * lpair_D(jj))))
                         % (4 * lpair_D(jj));
        end
    endfunction

    localparam integer CORE_LAT = (NTRIP > 0) ?
        (trip_rtl_lat(0) + trip_rtl_lat(1) + trip_rtl_lat(2))
        : 0;
    localparam integer LATENCY = CORE_LAT + lpair_rtl_lat(0)
                                 + ((NPAIRL > 1) ? lpair_rtl_lat(1) : 0) + 1;
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

    localparam [15:0] KPRE1 = trip_kpre(1);
    localparam [15:0] KPRE2 = trip_kpre(2);
    localparam [15:0] KPREL0 = lpair_kpre(0);
    localparam [15:0] KPREL1 = lpair_kpre(1);
    localparam integer S0 = (SCALING_PACK) & 3;
    localparam integer S1 = (SCALING_PACK >> 2) & 3;
    localparam integer S2 = (SCALING_PACK >> 4) & 3;

    fft_stage_r23 #(
        .DEPTH          (N >> 3),
        .WIDTH          (INTERN_WIDTH),
        .SIGMA0         (S0), .SIGMA1(S1), .SIGMA2(S2),
        .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
        .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
        .ROM_BASE       (0), .NPTS(8 * (N >> 3)),
        .INVERSE        (INVERSE), .Q8(q8_of(TWIDDLE_DECIMAL)), .K_PRELOAD(16'h0),
        .TWIDDLE_FILE   (TWIDDLE_FILE_T0)
    ) u_t0 ( .clk(clk), .ce(run), .rst(rst),
        .in_re(in_x_re), .in_im(in_x_im),
        .out_re(t1_re), .out_im(t1_im) );

    fft_stage_r23 #(
        .DEPTH          (N >> 6),
        .WIDTH          (INTERN_WIDTH),
        .SIGMA0         ((SCALING_PACK >> 6) & 3),
        .SIGMA1         ((SCALING_PACK >> 8) & 3),
        .SIGMA2         ((SCALING_PACK >> 10) & 3),
        .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
        .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
        .ROM_BASE       (0), .NPTS(8 * (N >> 6)),
        .INVERSE        (INVERSE), .Q8(q8_of(TWIDDLE_DECIMAL)), .K_PRELOAD(KPRE1[15:0]),
        .TWIDDLE_FILE   (TWIDDLE_FILE_T1)
    ) u_t1 ( .clk(clk), .ce(run), .rst(rst),
        .in_re(t1_re), .in_im(t1_im), .out_re(t2_re), .out_im(t2_im) );

    fft_stage_r23 #(
        .DEPTH          (N >> 9),
        .WIDTH          (INTERN_WIDTH),
        .SIGMA0         ((SCALING_PACK >> 12) & 3),
        .SIGMA1         ((SCALING_PACK >> 14) & 3),
        .SIGMA2         ((SCALING_PACK >> 16) & 3),
        .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
        .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
        .ROM_BASE       (0), .NPTS(8 * (N >> 9)),
        .INVERSE        (INVERSE), .Q8(q8_of(TWIDDLE_DECIMAL)), .K_PRELOAD(KPRE2[15:0]),
        .TWIDDLE_FILE   (TWIDDLE_FILE_T2)
    ) u_t2 ( .clk(clk), .ce(run), .rst(rst),
        .in_re(t2_re), .in_im(t2_im), .out_re(l0_re), .out_im(l0_im) );

    // ---------------- r2 leftover pairs ----------------
    wire signed [INTERN_WIDTH-1:0] l1_re, l1_im;
    wire signed [INTERN_WIDTH-1:0] core_w_re, core_w_im;
    generate
        if (NPAIRL > 0) begin : has_l0
            localparam integer D0   = lpair_D(0);
            localparam integer SG0  = (SCALING_PACK >> (2 * (3*NTRIP))) & 3;
            localparam integer SG1  = (SCALING_PACK >> (2 * (3*NTRIP+1))) & 3;
            fft_stage_r22 #(
                .DEPTH          (D0),
                .WIDTH          (INTERN_WIDTH),
                .SIGMA0         (SG0), .SIGMA1(SG1),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       (0), .NPTS(N),
                .INVERSE        (INVERSE),
                .K_PRELOAD      (KPREL0),
                .TWIDDLE_FILE   (TWIDDLE_FILE_L0)
            ) u_l0 ( .clk(clk), .ce(run), .rst(rst),
                .in_re(l0_re), .in_im(l0_im),
                .out_re(l1_re), .out_im(l1_im) );
        end else begin : no_l0
            assign l1_re = l0_re; assign l1_im = l0_im;
        end
        if (NPAIRL > 1) begin : has_l1
            localparam integer D1   = lpair_D(1);
            localparam integer SG2  = (SCALING_PACK >> (2 * (3*NTRIP+2))) & 3;
            localparam integer SG3  = (SCALING_PACK >> (2 * (3*NTRIP+3))) & 3;
            fft_stage_r22 #(
                .DEPTH          (D1),
                .WIDTH          (INTERN_WIDTH),
                .SIGMA0         (SG2), .SIGMA1(SG3),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       (0), .NPTS(N),
                .INVERSE        (INVERSE),
                .K_PRELOAD      (KPREL1),
                .TWIDDLE_FILE   (TWIDDLE_FILE_L1)
            ) u_l1 ( .clk(clk), .ce(run), .rst(rst),
                .in_re(l1_re), .in_im(l1_im),
                .out_re(core_w_re), .out_im(core_w_im) );
        end else begin : no_l1
            assign core_w_re = l1_re; assign core_w_im = l1_im;
        end
    endgenerate

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
