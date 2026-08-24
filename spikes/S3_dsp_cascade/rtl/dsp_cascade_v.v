// S3 spike (Verilog variant, uniform widths -- no resize breaks):
// signed MAC chain mixing +/-, like the FFT combine / Karatsuba tail.
module dsp_cascade_v (
    input  wire                 clk,
    input  wire signed [17:0]   a0, b0, a1, b1,
    input  wire signed [17:0]   a2, b2, a3, b3,
    output reg  signed [41:0]   y
);
    reg signed [35:0]  p0, p1, p2;
    reg signed [43:0]  acc;
    always @(posedge clk) begin
        p0  <= a0 * b0;
        p1  <= p0 + a1 * b1;    // cascade add
        p2  <= p1 - a2 * b2;    // cascade sub
        // final hop kept at the SAME 36-bit granularity as the chain
        acc <= {{8{p2[35]}}, p2} - ({{8{a3[17]}}, a3 * b3});
        y   <= acc;
    end
endmodule
