// S7 follow-up spike: does the 3-stage folded ternary rotate tree infer
// properly?  Expect per hop: one LUT compressor level + one CARRY8 chain,
// zero DSP48s, no register merging (worst path ~= one ternary add).
//
// re' = round((re+im) * q, TD),  im' = round((im-re) * q, TD)
// q = round(sqrt2/2 * 2^TD) = 92682 = 2^16+2^14+2^13+2^11+2^9+2^3+2^1
// (TD = 17): 14 shifted terms + the round bit = 3 ternary levels.
`default_nettype none

module rot45_unit #(
    parameter integer IW = 18,
    parameter integer TD = 17
)(
    input  wire                     clk,
    input  wire                     ce,
    input  wire signed [IW-1:0]     d_re,
    input  wire signed [IW-1:0]     d_im,
    output reg  signed [IW-1:0]     r_re,
    output reg  signed [IW-1:0]     r_im
);
    localparam integer OW = IW + TD + 2;   // product + round headroom

    wire signed [OW-1:0] re_w = d_re;      // sign-extend once
    wire signed [OW-1:0] im_w = d_im;
    wire signed [OW-1:0] round_c = {{(OW-TD-1){1'b0}}, 1'b1} <<< (TD-1);

    // L1: 4 ternary partials (3 shifted terms each); the 2^1 taps are
    // registered alongside so L2 pairs them with the L1 registers
    reg signed [OW-1:0] p1, p2, q1, q2, lo_re, lo_im;
    // L2: 2 ternary sums (A = re side, B = im side)
    reg signed [OW-1:0] a_s, b_s;

    always @(posedge clk) if (ce) begin
        // L1
        p1 <= (re_w <<< 16) + (re_w <<< 14) + (re_w <<< 13);
        p2 <= (re_w <<< 11) + (re_w <<< 9)  + (re_w <<< 3);
        q1 <= (im_w <<< 16) + (im_w <<< 14) + (im_w <<< 13);
        q2 <= (im_w <<< 11) + (im_w <<< 9)  + (im_w <<< 3);
        lo_re <= re_w <<< 1;
        lo_im <= im_w <<< 1;
        // L2
        a_s <= p1 + p2 + lo_re;
        b_s <= q1 + q2 + lo_im;
        // L3: round-half-up folded into the ternary add; >>> TD and the
        // truncation to IW are pure D-side wiring (wrap, no saturate --
        // production adds saturation at the consumer)
        r_re <= (a_s + b_s + round_c) >>> TD;
        r_im <= (b_s - a_s + round_c) >>> TD;
    end

endmodule

`default_nettype wire

module rot45_probe_top (
    input  wire                     clk,
    input  wire                     ce,
    input  wire signed [17:0]       in_re,
    input  wire signed [17:0]       in_im,
    output reg  signed [17:0]       out_re,
    output reg  signed [17:0]       out_im
);
    // two units, second input one clock later (the two rotate-class d's
    // of a group need not be simultaneous)
    reg signed [17:0] d2_re, d2_im;
    wire signed [17:0] r1_re, r1_im, r2_re, r2_im;

    rot45_unit #(.IW(18), .TD(17)) u0 (
        .clk(clk), .ce(ce), .d_re(in_re), .d_im(in_im),
        .r_re(r1_re), .r_im(r1_im));
    rot45_unit #(.IW(18), .TD(17)) u1 (
        .clk(clk), .ce(ce), .d_re(d2_re), .d_im(d2_im),
        .r_re(r2_re), .r_im(r2_im));

    always @(posedge clk) if (ce) begin
        d2_re <= in_re;  d2_im <= in_im;
        out_re <= r1_re + r2_re;   // consume, so nothing folds away
        out_im <= r1_im - r2_im;
    end

endmodule

`default_nettype wire
