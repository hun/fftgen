// S1 coding (b): single-port ring buffer, read-old/write-new at the SAME
// address on ONE port (the pairing trick from PLAN.md 2.7). Question: does
// this infer a RAMB36 or fall back to LUTRAM?

module ram_rw1 #(
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

    reg [AW-1:0] ptr;

    always @(posedge clk) begin
        if (ce) begin
            dout <= mem[ptr];               // read-old (nonblocking)
            mem[ptr] <= din;                // write-new, same address
            ptr <= ptr + {{(AW-1){1'b0}}, 1'b1};
        end
    end
endmodule
