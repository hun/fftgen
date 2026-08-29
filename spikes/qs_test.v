// multiply-form matrix: find which Verilog form computes a1(17b signed) * w(18b signed)
`default_nettype none
module qs (
    input wire clk,
    output wire signed [34:0] o_A,  // a1 * wq, no $signed
    output wire signed [34:0] o_B,  // $signed(a1) * $signed(wq)
    output wire signed [34:0] o_C,  // $signed(a1 * wq)
    output wire signed [34:0] o_D,  // explicit sign-extended operands
    output wire signed [34:0] o_E   // 35-bit cast operands
);
    localparam integer SW = 16;
    localparam integer TW = 18;
    localparam integer PW = SW + 1 + TW;

    reg signed [SW:0]   a1_re = -7625;
    reg signed [TW-1:0] wq_im = 131071;

    wire signed [PW-1:0] a1_35 = {{(PW-SW-1){a1_re[SW]}}, a1_re};
    wire signed [PW-1:0] wq_35 = {{(PW-TW){wq_im[TW-1]}}, wq_im};

    assign o_A = a1_re * wq_im;
    assign o_B = $signed(a1_re) * $signed(wq_im);
    assign o_C = $signed(a1_re * wq_im);
    assign o_D = a1_35 * wq_35;
    assign o_E = $signed(a1_35) * $signed(wq_35);
endmodule
`default_nettype wire
