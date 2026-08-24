// S1 coding (b-ultra): same as ram_rw1 but with ram_style="ultra".
// Probe: does the ultra hint alone put a same-address read-old/write-new
// ring buffer into URAM?
module ram_rw1_ultra #(
    parameter integer DEPTH = 8192,
    parameter integer WIDTH = 72
) (
    input  wire             clk,
    input  wire             ce,
    input  wire [WIDTH-1:0] din,
    output reg  [WIDTH-1:0] dout
);
    (* ram_style = "ultra" *)
    reg [WIDTH-1:0] mem [0:DEPTH-1];

    localparam AW = $clog2(DEPTH);

    reg [AW-1:0] ptr;

    always @(posedge clk) begin
        if (ce) begin
            dout <= mem[ptr];
            mem[ptr] <= din;
            ptr <= ptr + {{(AW-1){1'b0}}, 1'b1};
        end
    end
endmodule
