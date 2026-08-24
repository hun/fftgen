// S1 coding (c): TWO independent delay lines sharing ONE true-dual-port
// memory -- line X runs read-old/write-new on port A's address space, line Y
// on port B's. Question: does this infer exactly ONE RAMB36?

module ram_tdp_pair #(
    parameter integer DEPTH = 1024,
    parameter integer WIDTH = 32
) (
    input  wire             clk,
    input  wire             ce,
    input  wire [WIDTH-1:0] din_x,
    input  wire [WIDTH-1:0] din_y,
    output reg  [WIDTH-1:0] dout_x,
    output reg  [WIDTH-1:0] dout_y
);
    (* ram_style = "block" *)
    reg [WIDTH-1:0] mem [0:DEPTH-1];

    localparam AW = $clog2(DEPTH);

    reg [AW-1:0] ptr_x;
    reg [AW-1:0] ptr_y;

    // port A serves line X
    always @(posedge clk) begin
        if (ce) begin
            dout_x <= mem[ptr_x];
            mem[ptr_x] <= din_x;
            ptr_x <= ptr_x + {{(AW-1){1'b0}}, 1'b1};
        end
    end

    // port B serves line Y (independent pointer)
    always @(posedge clk) begin
        if (ce) begin
            dout_y <= mem[ptr_y];
            mem[ptr_y] <= din_y;
            ptr_y <= ptr_y + {{(AW-1){1'b0}}, 1'b1};
        end
    end
endmodule
