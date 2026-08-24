// S1 coding (a): SDP baseline -- dedicated write port (A) + read port (B).
// Functionally equivalent delay line: read address equals write address
// every cycle (read-old/write-new expressed through SEPARATE port logic),
// which is what a clean SDP mapping looks like.

module ram_sdp #(
    parameter integer DEPTH = 1024,
    parameter integer WIDTH = 32
) (
    input  wire             clk,
    input  wire             ce,
    input  wire [WIDTH-1:0] din,
    output reg  [WIDTH-1:0] dout
);
    (* ram_style = "block" *)
    reg [WIDTH-1:0] mem [0:DEPTH-1];

    localparam AW = $clog2(DEPTH);

    reg [AW-1:0] wptr;

    always @(posedge clk) begin
        if (ce) begin
            mem[wptr] <= din;               // port A: write
            wptr <= wptr + {{(AW-1){1'b0}}, 1'b1};
        end
    end

    always @(posedge clk) begin
        if (ce) begin
            dout <= mem[wptr];              // port B: sync read (+1 reg),
        end                                 // old value via nonblocking NBAs
    end
endmodule
