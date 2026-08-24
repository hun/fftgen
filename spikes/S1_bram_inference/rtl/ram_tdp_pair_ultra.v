// S1 coding (c-ultra): same as ram_tdp_pair (two delay lines, one per port,
// SAME address per port for read-old/write-new) but with ram_style="ultra".
// NOTE: both pointers reset to 0 -> cross-port read/write address collision
// every cycle. This is the FORBIDDEN pattern per the collision rule; kept
// only to document what synthesis does with it.
module ram_tdp_pair_ultra #(
    parameter integer DEPTH = 8192,
    parameter integer WIDTH = 72
) (
    input  wire             clk,
    input  wire             ce,
    input  wire [WIDTH-1:0] din_x,
    input  wire [WIDTH-1:0] din_y,
    output reg  [WIDTH-1:0] dout_x,
    output reg  [WIDTH-1:0] dout_y
);
    (* ram_style = "ultra" *)
    reg [WIDTH-1:0] mem [0:DEPTH-1];

    localparam AW = $clog2(DEPTH);

    reg [AW-1:0] ptr_x;
    reg [AW-1:0] ptr_y;

    always @(posedge clk) begin
        if (ce) begin
            dout_x <= mem[ptr_x];
            mem[ptr_x] <= din_x;
            ptr_x <= ptr_x + {{(AW-1){1'b0}}, 1'b1};
        end
    end

    always @(posedge clk) begin
        if (ce) begin
            dout_y <= mem[ptr_y];
            mem[ptr_y] <= din_y;
            ptr_y <= ptr_y + {{(AW-1){1'b0}}, 1'b1};
        end
    end
endmodule
