// fftgen -- R=1 R2^2 SDF core, P7 (DIF, native->bitreversed).
// Mirrors R22SDFGoldenModel / spikes/S5_r22/top_gen.py: pairs (2m,2m+1)
// merged into one 4-sample group with ONE shared complex multiply
// (3 products per 4-group), twiddle stride 4^m, D_m = N/4^{m+1}. Each
// pair stage outputs the value of stream position p at clock
// p + 3*D + 9 (the verified pipelined stage), so the chain re-aligns
// every pair's phase counter via K_PRELOAD = (-upstream_latency) mod
// 4D, and the odd-n leftover plain radix-2 stage (D=1, W^0 trivial,
// 0 DSPs) starts from its post-warm parity state (LO_PARITY).
//
// The twiddle ROM (write_r22_twiddle_mem) holds, per pair m, the three
// slices [T[g*4^m]], [T[2g*4^m]], [T[3g*4^m]] (3*D_m words, base
// sum(3*D_t, t<m)); an odd stage count appends one W^0 word for the
// leftover at base sum(3*D_t) over ALL pairs.
//
// Same AXI-stream framing contract as fft_sdf.v (tvalid/tuser/tlast
// ride at fixed latency LATENCY = sum(3*D+9) [+11 leftover] [+1 output
// register]; the datapath and marker chain advance only while
// ce && s_axis_tvalid).
//
// INVERSE consistency: INVERSE flips the intra-stage +/-j combines;
// the conjugate (IFFT) twiddle table must come from the ROM (write the
// file with cfg.inverse=True). The two always travel together -- the
// generator bakes both from one FFTConfig.

`default_nettype none

module fft_sdf_r22 #(
    parameter integer NUM_POINTS     = 64,
    parameter integer SAMPLE_WIDTH   = 16,
    parameter integer SAMPLE_DECIMAL = 0,
    parameter integer OUTPUT_WIDTH   = 16,
    parameter integer OUTPUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    parameter integer SCALING_PACK   = 32'h01010101,
    parameter integer INVERSE        = 0,
    // DIF only for now; DIT is a separate topology (fft_stage_r22_dit,
    // wired via TOPOLOGY in a later P7 step)
    parameter integer TOPOLOGY       = 0,
    parameter TWIDDLE_FILE           = "fft_twiddles_r22.mem",
    // internal datapath width; the generator emits
    // sample_width + max(0, num_stages - sum(shifts)) + 1 -- the +1
    // covers the sum-path carry. The default only fits the auto
    // schedule for N <= 32; standalone use must size it as above or
    // the internal truncations below can wrap.
    parameter integer INTERN_WIDTH   = SAMPLE_WIDTH + 5,
    parameter integer PIPE_DEPTH     = 10, // kept for compat, not used
    parameter [4095:0] PRELOAD_PACK  = 4096'b0, // unused in r22 (K_PRELOAD per pair)
    parameter integer TWIDDLE_MEM    = 0
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

    localparam integer N        = NUM_POINTS;
    localparam integer NSTAGES  = $clog2(N);
    localparam integer NPAIRS   = NSTAGES / 2;
    localparam integer LEFTOVER = NSTAGES % 2;

    // R2^2 chain latency helpers (per-pair RTL latency 3D+9, verified;
    // leftover plain D=1 stage adds D + NLAYERS = 11).
    function integer chain_lat;      // pairs only, upstream of `npairs'
        input integer npairs;
        integer mm, DD, ll;
        begin
            ll = 0;
            for (mm = 0; mm < npairs; mm = mm + 1) begin
                DD = N >> (2 * mm + 2);
                ll = ll + 3 * DD + 9;
            end
            chain_lat = ll;
        end
    endfunction
    // full datapath latency: pairs + odd-n leftover (D + NLAYERS = 11)
    localparam integer CORE_LAT = chain_lat(NSTAGES / 2)
                                  + ((NSTAGES % 2) ? 11 : 0);
    function integer up_lat;         // latency of the pairs upstream of g
        input integer gg;
        begin
            up_lat = chain_lat(gg);
        end
    endfunction
    function integer pair_base;      // twiddle ROM base of pair g
        input integer gg;
        integer mm, DD2, bb;
        begin
            bb = 0;
            for (mm = 0; mm < gg; mm = mm + 1) begin
                DD2 = N >> (2 * mm + 2);
                bb = bb + 3 * DD2;
            end
            pair_base = bb;
        end
    endfunction

    // +1: the registered quantizer output below
    localparam integer LATENCY = CORE_LAT + 1;
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
    // contract (PLAN.md 2.8): tvalid is low whenever the datapath is
    // frozen (run == 0), so a consumer never sees stale data twice
    assign m_axis_tvalid = out_valid_r && run;

    // frame marker sideband: shift register, depth LATENCY
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

    // stage chain: NPAIRS r22 stages + optional leftover plain stage
    localparam integer WIDX = (NPAIRS > 0) ? NPAIRS : 1; // avoid [0:-1]
    wire signed [INTERN_WIDTH-1:0] w_re [0:WIDX-1];
    wire signed [INTERN_WIDTH-1:0] w_im [0:WIDX-1];
    wire signed [INTERN_WIDTH-1:0] w_lo_re, w_lo_im;
    wire signed [INTERN_WIDTH-1:0] in_x_re, in_x_im;
    assign in_x_re = $signed({{(INTERN_WIDTH-SAMPLE_WIDTH){s_axis_tdata_re[SAMPLE_WIDTH-1]}}, s_axis_tdata_re});
    assign in_x_im = $signed({{(INTERN_WIDTH-SAMPLE_WIDTH){s_axis_tdata_im[SAMPLE_WIDTH-1]}}, s_axis_tdata_im});

    genvar g;
    generate
        for (g = 0; g < NPAIRS; g = g + 1) begin : pairs
            localparam integer D = N >> (2 * g + 2);
            localparam integer SIGMA0 = (SCALING_PACK >> (4 * g)) & 3;
            localparam integer SIGMA1 = (SCALING_PACK >> (4 * g + 2)) & 3;
            localparam integer K_PRE = (4 * D - (up_lat(g) % (4 * D))) % (4 * D);

            fft_stage_r22 #(
                .DEPTH          (D),
                .WIDTH          (INTERN_WIDTH),
                .SIGMA0         (SIGMA0),
                .SIGMA1         (SIGMA1),
                .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),
                .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE       (pair_base(g)),
                .NPTS           (N),
                .INVERSE        (INVERSE),
                .K_PRELOAD      (K_PRE),
                .TWIDDLE_FILE   (TWIDDLE_FILE)
            ) u_pair (
                .clk(clk), .ce(run), .rst(rst),
                .in_re  (g == 0 ? in_x_re : w_re[g-1]),
                .in_im  (g == 0 ? in_x_im : w_im[g-1]),
                .out_re (w_re[g]), .out_im (w_im[g])
            );
        end
    endgenerate

    // leftover plain stage D=1 for odd NSTAGES. The chain outputs at
    // position p land on this stage's input at clock p + chain_lat; the
    // D=1 COMPUTE/PASS phase must therefore start flipped by
    // chain_lat mod 2 (verified in top_gen._leftover_preload / the
    // golden model's parity stepping). All other post-warm fields
    // follow the same parity: wptr=pwp=parity, raddr=(wptr-D) mod 2D.
    wire signed [INTERN_WIDTH-1:0] chain_out_re, chain_out_im;
    localparam integer LO_PARITY = chain_lat(NPAIRS) % 2;
    generate
        if (LEFTOVER != 0) begin : has_leftover
            if (NPAIRS == 0) begin : lo_direct
                assign chain_out_re = in_x_re;
                assign chain_out_im = in_x_im;
            end else begin : lo_chain
                assign chain_out_re = w_re[NPAIRS-1];
                assign chain_out_im = w_im[NPAIRS-1];
            end
            fft_stage #(
                .DEPTH(1), .WIDTH(INTERN_WIDTH),
                .SHIFT((SCALING_PACK >> (2 * (NSTAGES - 1))) & 3),
                .TWIDDLE_WIDTH(TWIDDLE_WIDTH), .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),
                .ROM_BASE(pair_base(NPAIRS)), .NPTS(N),
                .PRELOAD_I(0), .PRELOAD_C(LO_PARITY),
                .WPTR_PRE(LO_PARITY), .PWP_PRE(LO_PARITY),
                .RADDR_PRE(1 - LO_PARITY), .PIPE_PRE(0),
                .TOPOLOGY(0), .TRIVIAL(1), .TWIDDLE_MEM(TWIDDLE_MEM),
                .TWIDDLE_FILE(TWIDDLE_FILE)
            ) u_left (
                .clk(clk), .ce(run), .rst(rst),
                .in_re(chain_out_re), .in_im(chain_out_im),
                .out_re(w_lo_re), .out_im(w_lo_im)
            );
        end else begin : no_leftover
            assign w_lo_re = w_re[NPAIRS-1];
            assign w_lo_im = w_im[NPAIRS-1];
        end
    endgenerate

    wire signed [INTERN_WIDTH-1:0] core_re, core_im;
    generate
        if (LEFTOVER != 0) begin : sel_left
            assign core_re = w_lo_re;
            assign core_im = w_lo_im;
        end else if (NPAIRS > 0) begin : sel_pairs
            assign core_re = w_re[NPAIRS-1];
            assign core_im = w_im[NPAIRS-1];
        end else begin : sel_pass
            assign core_re = in_x_re;
            assign core_im = in_x_im;
        end
    endgenerate

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
