// fftgen -- timing-vehicle wrapper for SSR OOC P&R runs.
//
// The SSR top exposes R*16-bit axis buses; on xcku5p-ffva676 that is
// 519 I/O ports for R=8, which no KU5P package can place. For timing
// verification the interesting paths are all INTERNAL (lane DSP
// cascades, crossbar layers), so this wrapper drives fft_ssr from an
// internal counter/LFSR and sinks its output, exposing only
// {clk, rst, done}. Timing on fft_ssr's internal paths is identical to
// the real top; only pad delays differ (not in the critical loop for
// 500 MHz cores).
//
// Parameters mirror fft_ssr's; the wrapper replicates the generator's
// stimulus convention: R samples/cycle, SOF on lane 0 of a frame's
// first word, EOF on lane R-1 of the last word.
`default_nettype none
module ssr_timing_wrap #(
    parameter integer NUM_POINTS     = 16,
    parameter integer SSR            = 8,
    parameter integer SAMPLE_WIDTH   = 16,
    parameter integer SAMPLE_DECIMAL = 0,
    parameter integer OUTPUT_WIDTH   = 16,
    parameter integer OUTPUT_DECIMAL = 0,
    parameter integer TWIDDLE_WIDTH  = 18,
    parameter integer TWIDDLE_DECIMAL = TWIDDLE_WIDTH - 1,
    parameter integer SCALING_PACK   = 32'h01010101,
    parameter integer INTERN_WIDTH   = SAMPLE_WIDTH + 5,
    parameter integer PIPE_DEPTH     = 7,
    parameter         WN_FILE        = "fft_wn.mem",
    parameter integer INVERSE        = 0
)(
    input  wire clk,
    input  wire rst,
    output reg  done
);
    localparam N = NUM_POINTS;
    localparam R = SSR;
    localparam M = N / R;
    localparam SW = SAMPLE_WIDTH;
    localparam OW = OUTPUT_WIDTH;

    reg [R*SW-1:0] din_re;
    reg [R*SW-1:0] din_im;
    reg            tv;
    reg            tu, tl;
    wire           ov;
    wire signed [R*OW-1:0] ore;
    wire signed [R*OW-1:0] oim;

    reg [15:0] wcnt;
    integer g;          // word counter within the run
    reg [7:0]  lfsr = 8'h01;
    reg [31:0] done_cnt;
    reg [31:0] chk;
    always @(posedge clk) begin
        if (rst) begin
            wcnt <= 0;
            done_cnt <= 0;
            done <= 0;
            chk <= 0;
        end else begin
            // continuous random-ish stimulus; no backpressure
            lfsr <= {lfsr[6:0], lfsr[7] ^ lfsr[5] ^ lfsr[4] ^ lfsr[3]};
            tv <= 1'b1;
            for (g = 0; g < R; g = g + 1) begin
                din_re[g*SW +: SW] <= {lfsr[6:0], lfsr[7], lfsr[0],
                                       lfsr[2], lfsr[5], lfsr[1], lfsr[3],
                                       lfsr[4], lfsr[6], lfsr[0], lfsr[7],
                                       lfsr[1], lfsr[2], lfsr[3], lfsr[5],
                                       lfsr[4]} ^ (g * 8'h13);
                din_im[g*SW +: SW] <= ~{lfsr[3:0], lfsr[7:4], lfsr[1:0],
                                        lfsr[7:2]} ^ (g * 8'h2b);
            end
            tu <= (wcnt % M == 0);
            tl <= (wcnt % M == M - 1);
            wcnt <= wcnt + 16'd1;
            if (ov) begin
                done_cnt <= done_cnt + 32'd1;
                chk <= chk + ore + oim;
                if (done_cnt > 64)
                    done <= |chk;
            end
        end
    end

    fft_ssr #(
        .NUM_POINTS      (N),
        .SSR             (R),
        .SAMPLE_WIDTH    (SW),
        .SAMPLE_DECIMAL  (SAMPLE_DECIMAL),
        .OUTPUT_WIDTH    (OW),
        .OUTPUT_DECIMAL  (OUTPUT_DECIMAL),
        .TWIDDLE_WIDTH   (TWIDDLE_WIDTH),
        .TWIDDLE_DECIMAL (TWIDDLE_DECIMAL),
        .SCALING_PACK    (SCALING_PACK),
        .INTERN_WIDTH    (INTERN_WIDTH),
        .PIPE_DEPTH      (PIPE_DEPTH),
        .WN_FILE         (WN_FILE),
        .INVERSE         (INVERSE)
    ) u_ssr (
        .clk           (clk),
        .ce            (1'b1),
        .rst           (rst),
        .s_axis_tvalid (tv),
        .s_axis_tdata_re(din_re),
        .s_axis_tdata_im(din_im),
        .s_axis_tuser  (tu),
        .s_axis_tlast  (tl),
        .m_axis_tvalid (ov),
        .m_axis_tdata_re(ore),
        .m_axis_tdata_im(oim)
    );
endmodule
`default_nettype wire
