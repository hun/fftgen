// S1 coding (d): SDP-style deep delay line with INDEPENDENT read pointer
// (read addr != write addr). Probe: does this unlock URAM inference for
// very deep lines on UltraScale+?

module ram_sdp_deep #(
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

    reg [AW-1:0] wptr;
    reg [AW-1:0] rptr;

    always @(posedge clk) begin
        if (ce) begin
            mem[wptr] <= din;
            wptr <= wptr + {{(AW-1){1'b0}}, 1'b1};
        end
    end

    always @(posedge clk) begin
        if (ce) begin
            dout <= mem[rptr];
            rptr <= rptr + {{(AW-1){1'b0}}, 1'b1};
        end
    end
endmodule
