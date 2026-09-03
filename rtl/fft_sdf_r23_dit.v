// fftgen -- the R2^3 DIT pipeline (S8): bit-reversed input, natural
// output -- the r23 IFFT core.  Mirror of fft_sdf_r23.v (the DIF core).
//
// Chain (finest layer first, the DIF order reversed):
//   R = NSTAGES mod 3 plain radix-2 DIT leftovers (fft_sdf.v fft_stage,
//       TOPOLOGY=1, depths 2^s, s = 0..R-1), then MMAX = NSTAGES/3
//       radix-2^3 DIT triples (fft_stage_r23_dit), m = MMAX-1 .. 0.
// Shift schedule: layer L in processing order uses the 2-bit field L of
// SCALING_PACK (one shift per r2 layer, three per triple) -- the same
// pipeline-order convention as the validated r22 DIT chain.
//
// Twiddle ROM (one shared file): the leftover slices first
//   word (2^s - 1) + j                = T[j * N/2^(s+1)],  j < 2^s
// (the fft_sdf DIT ROM_BASE = SUM_D convention), then the triples in
// window-ordered slices
//   word TBASE_j + (w-1)*G_j + g      = T[bitrev3(w)*g*8^m_j]
// with TBASE_j = (R ? 2^R - 1 : 0) + 7*sum_{t<j} G_t.
//
// K_PRELOAD (the triple k-window phase): the golden's triple j sees the
// stream phase pos - up_j, so the RTL k preloads -up_j mod 8*G_j (the
// KP_T* overrides exist for the bring-up scan; -1 = auto).
// The r2 leftover FSM/pointer preloads mirror the golden's phase
// preloads: warm = (-sum_{t<s}(2^t + 10)) mod 2^(s+1) steps of step(0,0)
// (the bring-up generator reads the exact post-warm golden state).

`default_nettype none

module fft_sdf_r23_dit #(
    parameter integer NUM_POINTS      = 8192,
    parameter integer SAMPLE_WIDTH    = 16,
    parameter integer INTERN_WIDTH    = 16,
    parameter integer TWIDDLE_WIDTH   = 18,
    parameter integer TWIDDLE_DECIMAL = 17,
    parameter [63:0]  SCALING_PACK    = 64'h0, // 2 bits per layer, layer 0 = LSB
    parameter integer INVERSE         = 1,
    parameter TWIDDLE_FILE            = "fft_tw_r23_dit.mem",
    parameter integer Q8              = 92682,
    // per-triple K_PRELOAD override (-1 = auto); the bring-up scan
    parameter integer KP_T0           = -1,
    parameter integer KP_T1           = -1,
    parameter integer KP_T2           = -1,
    // the second r2 leftover's (s'=1) post-warm state overrides: the
    // bring-up generator reads them from the golden's phase preload
    // (the internal WARM formula covers the FSM phase but not the
    // 9-layer pipe history)
    parameter [15:0]  R2B_I           = 16'hffff,  // 0xffff = use WARM
    parameter         R2B_C           = 1'h0,
    parameter [15:0]  R2B_WPTR        = 16'hffff,
    parameter [15:0]  R2B_PWP         = 16'hffff,
    parameter [15:0]  R2B_RADDR       = 16'hffff,
    parameter [8:0]   R2B_PIPE        = 9'h0
)(
    input  wire                      clk,
    input  wire                      ce,
    input  wire                      s_axis_tvalid,
    input  wire [SAMPLE_WIDTH-1:0]   s_axis_tdata_re,
    input  wire [SAMPLE_WIDTH-1:0]   s_axis_tdata_im,
    input  wire                      s_axis_tuser,
    input  wire                      s_axis_tlast,
    output wire                      m_axis_tvalid,
    output wire [INTERN_WIDTH-1:0]   m_axis_tdata_re,
    output wire [INTERN_WIDTH-1:0]   m_axis_tdata_im,
    output wire                      m_axis_tuser,
    output wire                      m_axis_tlast,
    input  wire                      rst
);

    localparam integer N      = NUM_POINTS;
    localparam integer NSTAGES = $clog2(N);
    localparam integer MMAX    = NSTAGES / 3;
    localparam integer R       = NSTAGES - 3 * MMAX;
    localparam integer IW      = INTERN_WIDTH;

    initial if (MMAX == 0)
        $error("fft_sdf_r23_dit: no triples for N=%0d (NSTAGES=%0d)",
               N, NSTAGES);

    // ---------------- latencies (RTL = golden here) -------------------
    // r2 DIT leftover s: D + 10 pipeline layers; triple j: 7*G_j + 1
    function integer r2_lat;
        input integer s;
        begin
            r2_lat = (1 << s) + 10;
        end
    endfunction
    function integer trip_G;
        input integer j;                   // 0 = finest triple
        begin
            trip_G = N >> (3 * (MMAX - 1 - j) + 3);
        end
    endfunction
    function integer trip_lat;
        input integer j;
        begin
            trip_lat = 7 * trip_G(j) + 1;
        end
    endfunction
    // upstream latency before triple j (the r2 leftovers + triples < j)
    function integer up_lat;
        input integer j;
        integer s, acc;
        begin
            acc = 0;
            for (s = 0; s < R; s = s + 1)
                acc = acc + r2_lat(s);
            for (s = 0; s < j; s = s + 1)
                acc = acc + trip_lat(s);
            up_lat = acc;
        end
    endfunction
    function integer trip_kp;
        input integer j;
        input integer trim;
        integer gp, up;
        begin
            gp = 8 * trip_G(j);
            up = up_lat(j) + ((trim < 0) ? 0 : trim);
            trip_kp = (gp - (up % gp)) % gp;
        end
    endfunction
    // triple j's ROM base: the leftover slices then 7*G per triple
    function integer trip_rom_base;
        input integer j;
        integer s, acc;
        begin
            acc = (R > 0) ? ((1 << R) - 1) : 0;
            for (s = 0; s < j; s = s + 1)
                acc = acc + 7 * trip_G(s);
            trip_rom_base = acc;
        end
    endfunction
    function integer total_lat;
        input integer dummy;
        integer s, acc;
        begin
            acc = 0;
            for (s = 0; s < R; s = s + 1)
                acc = acc + r2_lat(s);
            for (s = 0; s < MMAX; s = s + 1)
                acc = acc + trip_lat(s);
            total_lat = acc;
        end
    endfunction

    localparam integer LAT = total_lat(0);
    localparam integer CNT_W = $clog2(LAT + 1);

    // ---------------- valid + marker sideband -------------------------
    wire run = ce && s_axis_tvalid;
    reg [CNT_W-1:0] cnt;
    reg             out_valid_r;
    always @(posedge clk) begin
        if (rst) begin
            cnt         <= {CNT_W{1'b0}};
            out_valid_r <= 1'b0;
        end else if (run) begin
            if (cnt != LAT[CNT_W-1:0])
                cnt <= cnt + {{(CNT_W-1){1'b0}}, 1'b1};
            if (cnt == LAT[CNT_W-1:0] - 1'b1)
                out_valid_r <= 1'b1;
        end
    end
    assign m_axis_tvalid = out_valid_r && run;

    reg mk_user [0:LAT-1];
    reg mk_last [0:LAT-1];
    integer k;
    always @(posedge clk) begin
        if (run) begin
            mk_user[0] <= s_axis_tuser;
            mk_last[0] <= s_axis_tlast;
            for (k = 1; k < LAT; k = k + 1) begin
                mk_user[k] <= mk_user[k-1];
                mk_last[k] <= mk_last[k-1];
            end
        end
    end
    assign m_axis_tuser = mk_user[LAT-1];
    assign m_axis_tlast = mk_last[LAT-1];

    // ---------------- stage chain -------------------------------------
    wire signed [IW-1:0] st_out_re [0:MMAX-1];
    wire signed [IW-1:0] st_out_im [0:MMAX-1];
    // the r2 leftover outputs: wires for R stages (R <= 2)
    wire signed [IW-1:0] r2_out_re [0:2];
    wire signed [IW-1:0] r2_out_im [0:2];

    // the first data stage's input (sample-width extended)
    wire signed [IW-1:0] first_re = {{(IW-SAMPLE_WIDTH){s_axis_tdata_re[SAMPLE_WIDTH-1]}},
                                     s_axis_tdata_re};
    wire signed [IW-1:0] first_im = {{(IW-SAMPLE_WIDTH){s_axis_tdata_im[SAMPLE_WIDTH-1]}},
                                     s_axis_tdata_im};

    genvar g;
    generate
        // ---- plain radix-2 DIT leftovers (finest first) ----
        for (g = 0; g < R; g = g + 1) begin : r2s
            // the golden's warm state (the bring-up generator passes the
            // exact post-preload phase via these parameters; the defaults
            // reproduce warm = (-sum lat) mod 2D for the FSM)
            localparam integer D   = 1 << g;
            // warm = (-sum_{t<g}(2^t + 10)) mod 2D = -((2^g-1) + 10g) mod 2D
            // (correct for the r <= 2 range: at most two leftovers)
            localparam integer WARM = ((2*D)
                                       - (((1 << g) - 1) + 10*g) % (2*D)) % (2*D);
            fft_stage #(
                .DEPTH          (D),
                .WIDTH          (IW),
                .SHIFT          ((SCALING_PACK >> (2*g)) & 3),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       ((1 << g) - 1),
                .NPTS           (N),
                .PRELOAD_I      ((g == 1 && R2B_I != 16'hffff) ?
                                 R2B_I[15:0] : WARM % D),
                .PRELOAD_C      ((g == 1 && R2B_I != 16'hffff) ?
                                 R2B_C : ((WARM >= D) ? 1'b1 : 1'b0)),
                .WPTR_PRE       ((g == 1 && R2B_WPTR != 16'hffff) ?
                                 R2B_WPTR : WARM[15:0]),
                .PWP_PRE        ((g == 1 && R2B_PWP != 16'hffff) ?
                                 R2B_PWP : ((WARM + 2*D) & 16'hffff)),
                .RADDR_PRE      ((g == 1 && R2B_RADDR != 16'hffff) ?
                                 R2B_RADDR : (((WARM - D) % (2*D)) & 16'hffff)),
                .PIPE_PRE       ((g == 1) ? R2B_PIPE : 9'h0),
                .TOPOLOGY       (1),
                .TRIVIAL        (g == 0),
                .TWIDDLE_FILE   (TWIDDLE_FILE)
            ) u_r2 (
                .clk    (clk),
                .ce     (run),
                .rst    (rst),
                .in_re  (g == 0 ? first_re :
                         $signed(r2_out_re[g-1])),
                .in_im  (g == 0 ? first_im :
                         $signed(r2_out_im[g-1])),
                .out_re (r2_out_re[g]),
                .out_im (r2_out_im[g])
            );
        end

        // ---- r23 DIT triples, m descending (finest first) ----
        for (g = 0; g < MMAX; g = g + 1) begin : trips
            localparam integer GJ  = trip_G(g);
            localparam integer KP  = (g == 0) ?
                ((KP_T0 < 0) ? trip_kp(0, 0) : KP_T0) :
                (g == 1) ?
                ((KP_T1 < 0) ? trip_kp(1, 0) : KP_T1) :
                ((KP_T2 < 0) ? trip_kp(2, 0) : KP_T2);
            localparam [5:0] SH0 = (SCALING_PACK >> (2*(R+3*g))) & 3;
            localparam [5:0] SH1 = (SCALING_PACK >> (2*(R+3*g+1))) & 3;
            localparam [5:0] SH2 = (SCALING_PACK >> (2*(R+3*g+2))) & 3;
            fft_stage_r23_dit #(
                .DEPTH          (GJ),
                .WIDTH          (IW),
                .SIGMA0         (SH0),
                .SIGMA1         (SH1),
                .SIGMA2         (SH2),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       (trip_rom_base(g)),
                .NPTS           (N),
                .INVERSE        (INVERSE),
                .Q8             (Q8),
                .K_PRELOAD      (KP[15:0]),
                .TWIDDLE_FILE   (TWIDDLE_FILE)
            ) u_trip (
                .clk    (clk),
                .ce     (run),
                .rst    (rst),
                .in_re  (g == 0 ? (R > 0 ? $signed(r2_out_re[R-1])
                                         : first_re) :
                         $signed(st_out_re[g-1])),
                .in_im  (g == 0 ? (R > 0 ? $signed(r2_out_im[R-1])
                                         : first_im) :
                         $signed(st_out_im[g-1])),
                .out_re (st_out_re[g]),
                .out_im (st_out_im[g])
            );
        end
    endgenerate

    assign m_axis_tdata_re = st_out_re[MMAX-1];
    assign m_axis_tdata_im = st_out_im[MMAX-1];

endmodule

`default_nettype wire
