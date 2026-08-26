// fftgen -- streaming bit-reversal reorder buffer (ping-pong, PLAN.md 2.3)
//
// Converts a stream's per-frame slot order between natural and bit-reversed.
// Used after the core when the outer output order differs from the core's
// natural output order (native->native via DIF, bitrev->bitrev via DIT).
//
// Structure: two N-deep buffers (ping-pong). Frame f is written into buffer
// f%2 at natural addresses; frame f-1 is read from the other buffer at
// bit-reversed addresses. The separate buffers keep read and write
// addresses structurally disjoint (collision rule, PLAN.md 2.7) and give a
// clean frame boundary. Memory: 2N words (matches the golden model).
//
// Latency: N enabled cycles (first frame must complete before reads start).

`default_nettype none

module fft_reorder #(
    parameter integer NUM_POINTS = 16,
    parameter integer WIDTH      = 16
)(
    input  wire                     clk,
    input  wire                     ce,          // advances on ce && tvalid
    input  wire                     s_axis_tvalid,
    input  wire signed [WIDTH-1:0]  s_axis_tdata_re,
    input  wire signed [WIDTH-1:0]  s_axis_tdata_im,
    input  wire                     s_axis_tuser,
    input  wire                     s_axis_tlast,

    output wire                     m_axis_tvalid,
    output wire signed [WIDTH-1:0]  m_axis_tdata_re,
    output wire signed [WIDTH-1:0]  m_axis_tdata_im,
    output wire                     m_axis_tuser,
    output wire                     m_axis_tlast,

    input  wire                     rst          // sync, control only
);

    localparam integer N  = NUM_POINTS;
    localparam integer NW = $clog2(N);           // position within a frame
    localparam integer AW = NW + 1;              // + frame-parity bit
    localparam integer NN = N - 1;

    wire run = ce && s_axis_tvalid;

    // two N-deep buffers, selected by frame parity. Style policy as in
    // fft_stage (doc/mem_cutoffs.md S3): write/read addresses are the
    // frame-parity halves -- structurally disjoint, SDP-safe.
    localparam integer MEM_BITS = 2 * N * WIDTH;
    localparam integer MEM_STYLE = (MEM_BITS <= 1024) ? 0 :
                                   (MEM_BITS < 262144) ? 1 : 2;
    generate
        if (MEM_STYLE == 0) begin : g_mem
            (* ram_style = "distributed" *)
            reg signed [WIDTH-1:0] mem_re [0:2*N-1];
            (* ram_style = "distributed" *)
            reg signed [WIDTH-1:0] mem_im [0:2*N-1];
        end else if (MEM_STYLE == 1) begin : g_mem
            (* ram_style = "block" *)
            reg signed [WIDTH-1:0] mem_re [0:2*N-1];
            (* ram_style = "block" *)
            reg signed [WIDTH-1:0] mem_im [0:2*N-1];
        end else begin : g_mem
            (* ram_style = "ultra" *)
            reg signed [WIDTH-1:0] mem_re [0:2*N-1];
            (* ram_style = "ultra" *)
            reg signed [WIDTH-1:0] mem_im [0:2*N-1];
        end
    endgenerate
    reg mk_user [0:2*N-1];   // 1 b/entry: stays distributed at any depth
    reg mk_last [0:2*N-1];

    reg [NW-1:0] wpos;                // natural position within frame
    reg          frame_parity;        // which buffer the current frame fills
    reg          have_prev;           // a previous frame is available to read
    reg [NW-1:0] cycles;              // enabled-cycle count (fill gate)

    function [NW-1:0] bitrev_aw;
        input [NW-1:0] v;
        integer i;
        begin
            for (i = 0; i < NW; i = i + 1)
                bitrev_aw[NW-1-i] = v[i];
        end
    endfunction

    // read the PREVIOUS frame's slot: other buffer + bitrev(position)
    wire [AW-1:0] waddr = {{(1){frame_parity}}, wpos};
    wire [AW-1:0] raddr = {{(1){~frame_parity}}, bitrev_aw(wpos)};

    reg m_valid_r;
    reg signed [WIDTH-1:0] m_re_r, m_im_r;
    reg m_user_r, m_last_r;

    always @(posedge clk) begin
        if (rst) begin
            wpos         <= {NW{1'b0}};
            frame_parity <= 1'b0;
            have_prev    <= 1'b0;
            cycles       <= {NW{1'b0}};
            m_valid_r    <= 1'b0;
            m_re_r       <= {WIDTH{1'b0}};
            m_im_r       <= {WIDTH{1'b0}};
            m_user_r     <= 1'b0;
            m_last_r     <= 1'b0;
        end else if (run) begin
            // write current sample into the current buffer at natural pos
            g_mem.mem_re[waddr] <= s_axis_tdata_re;
            g_mem.mem_im[waddr] <= s_axis_tdata_im;
            mk_user[waddr] <= s_axis_tuser;
            mk_last[waddr] <= s_axis_tlast;
            // read previous frame's sample at bit-reversed position
            m_re_r   <= g_mem.mem_re[raddr];
            m_im_r   <= g_mem.mem_im[raddr];
            m_user_r <= mk_user[raddr];
            m_last_r <= mk_last[raddr];
            m_valid_r <= have_prev;
            cycles <= cycles + {{(NW-1){1'b0}}, 1'b1};
            if (cycles == NN[NW-1:0])
                have_prev <= 1'b1;
            wpos <= wpos + {{(NW-1){1'b0}}, 1'b1};
            if (wpos == NN[NW-1:0]) begin
                wpos         <= {NW{1'b0}};
                frame_parity <= ~frame_parity;
            end
        end
        // frozen cycles: everything holds
    end

    // contract (PLAN.md 2.8): tvalid low whenever the datapath is frozen
    assign m_axis_tvalid   = m_valid_r && run;
    assign m_axis_tdata_re = m_re_r;
    assign m_axis_tdata_im = m_im_r;
    assign m_axis_tuser    = m_user_r;
    assign m_axis_tlast    = m_last_r;

endmodule

`default_nettype wire
