"""S7 r2^3 spike -- generates the W8 (45-degree) probe stage variants from
the proven S5 `nat` stage (spikes/S5_r22/dsp_probe/variants/fft_stage_nat.v,
bit-exact + timing-MET reference).

Each variant inserts the 45-degree fabric constant multiply
    re' = (re + im)*q >> td,  im' = (im - re)*q >> td
(q = round_half_up(sqrt2/2 * 2^td), 5-7 taps -> ~3 CARRY8 levels) at a
different point of the r22 pipeline, unconditionally on the worst-case class
so Vivado cannot fold it away.  Timing probe only -- values are NOT the
r2^3 contract (that is the golden model's job); the probe answers WHERE the
rotation fits at 2 ns:

  w8_pre        operand-side, combinational into the existing AREG capture
  w8_pre_pipe   operand-side + one own register before the AREG
  w8_post       product-side (after DSP PREG), into the existing shift stage
  w8_post_pipe  product-side tap tree + one own register, shift in next hop

All variants keep the module name fft_stage_r22 (probe top unchanged) and
grow the stage latency by 0 / 0 / 0 / +1 respectively -- schedule neutrality
is NOT claimed; that re-derivation is golden-model work.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
NAT = os.path.abspath(os.path.join(
    HERE, "..", "S5_r22", "dsp_probe", "variants", "fft_stage_nat.v"))

BASE = open(NAT).read()

Q8_PARAM = """    parameter [15:0] K_PRELOAD       = 16'h0, // phase offset (-upstream mod 4D)
    parameter integer Q8             = 92682, // S7 probe: round(sqrt2/2 * 2^td), Q(td)
    parameter TWIDDLE_FILE           = "fft_twiddles_r22.mem"
"""


def patch(text, old, new, tag):
    assert text.count(old) == 1, f"patch anchor not unique: {tag}"
    return text.replace(old, new)


# ----------------------------------------------------------------------
# shared: the W8 fabric constant multiply (explicit shift-add tree, so
# nothing maps onto a DSP; use_dsp=no belt-and-braces on the outputs)
# ----------------------------------------------------------------------
def add_tree(text, fname, width, in_wires, out_wires, expr_a, expr_b):
    """Insert a mul_q8 function + rotate wires after a given anchor."""
    block = f"""
    // ---- S7 W8 probe: 45-degree fabric rotation (constant q, shift-add
    // tree -- generalized trivial_prod; the sign pattern here is W8^1 =
    // (sqrt2/2)(1-j): re' = (re+im)*q >> td, im' = (im-re)*q >> td) ----
    localparam integer W45 = {width} + 1 + TWIDDLE_WIDTH;
    function signed [W45-1:0] {fname};
        input signed [W45-1:0] v;
        integer b;
        reg signed [W45-1:0] acc;
        begin
            acc = {{W45{{1'b0}}}};
            for (b = 0; b < TWIDDLE_WIDTH; b = b + 1)
                if (Q8[b]) acc = acc + (v <<< b);
            {fname} = acc;
        end
    endfunction
    wire signed [W45-1:0] r45a_in = {expr_a};
    wire signed [W45-1:0] r45b_in = {expr_b};
    (* use_dsp = "no" *) wire signed [W45-1:0] r45a_q = {fname}(r45a_in);
    (* use_dsp = "no" *) wire signed [W45-1:0] r45b_q = {fname}(r45b_in);
"""
    return patch(text, in_wires, block + in_wires, out_wires)


def add_q8_param(text):
    return patch(text,
                 '    parameter [15:0] K_PRELOAD       = 16\'h0, // phase offset (-upstream mod 4D)\n'
                 '    parameter TWIDDLE_FILE           = "fft_twiddles_r22.mem"',
                 Q8_PARAM, "q8 param")


# ----------------------------------------------------------------------
# w8_pre: rotate the k3-muxed OPERAND, combinational into the AREG
# ----------------------------------------------------------------------
PRE = add_q8_param(BASE)
PRE = add_tree(
    PRE, "mul_q8o", "CB",
    "    reg signed [CB-1:0] m_r_re, m_r_im;           // the AREG (operand)",
    "pre tree",
    "$signed({{(W45-CB){m_re[CB-1]}}, m_re})"
    " + $signed({{(W45-CB){m_im[CB-1]}}, m_im})",
    "$signed({{(W45-CB){m_im[CB-1]}}, m_im})"
    " - $signed({{(W45-CB){m_re[CB-1]}}, m_re})")
PRE = patch(
    PRE,
    "    reg signed [CB-1:0] m_r_re, m_r_im;           // the AREG (operand)",
    """    // the class gate mirrors the r2^3 deepest-triple duty: the c1/c3
    // classes rotate, the sd (y2) class passes through untouched
    wire w8_class = (k3 < ONE_D) || (k3 >= THREE_D);
    // round-half-up >> td back to CB (wraps extreme values -- probe only)
    wire signed [CB-1:0] r45_re = (r45a_q
        + $signed({{(W45-1){1'b0}}, 1'b1} <<< (TWIDDLE_DECIMAL-1)))
        >>> TWIDDLE_DECIMAL;
    wire signed [CB-1:0] r45_im = (r45b_q
        + $signed({{(W45-1){1'b0}}, 1'b1} <<< (TWIDDLE_DECIMAL-1)))
        >>> TWIDDLE_DECIMAL;
    wire signed [CB-1:0] w8_sel_re = w8_class ? r45_re : $signed(m_re);
    wire signed [CB-1:0] w8_sel_im = w8_class ? r45_im : $signed(m_im);
    reg signed [CB-1:0] m_r_re, m_r_im;           // the AREG (operand)""",
    "pre regs")
PRE = patch(PRE,
            "            m_r_re <= m_re;   m_r_im <= m_im;",
            "            m_r_re <= w8_sel_re;   m_r_im <= w8_sel_im;",
            "pre capture")

# w8_pre_pipe: +1 register between the rotate and the AREG
PREP = patch(
    PRE,
    "    wire signed [CB-1:0] w8_sel_re = w8_class ? r45_re : $signed(m_re);\n"
    "    wire signed [CB-1:0] w8_sel_im = w8_class ? r45_im : $signed(m_im);\n"
    "    reg signed [CB-1:0] m_r_re, m_r_im;           // the AREG (operand)",
    "    wire signed [CB-1:0] w8_sel_re = w8_class ? r45_re : $signed(m_re);\n"
    "    wire signed [CB-1:0] w8_sel_im = w8_class ? r45_im : $signed(m_im);\n"
    "    reg signed [CB-1:0] w8q_re, w8q_im;   // S7 probe: rotate output reg\n"
    "    reg signed [CB-1:0] m_r_re, m_r_im;           // the AREG (operand)",
    "pre pipe regs")
PREP = patch(PREP,
             "            m_r_re <= w8_sel_re;   m_r_im <= w8_sel_im;",
             "            w8q_re <= w8_sel_re;   w8q_im <= w8_sel_im;\n"
             "            m_r_re <= w8q_re;      m_r_im <= w8q_im;",
             "pre pipe capture")
PREP = patch(PREP,
             "            m_r_re <= {CB{1'b0}}; m_r_im <= {CB{1'b0}};",
             "            w8q_re <= {CB{1'b0}}; w8q_im <= {CB{1'b0}};\n"
             "            m_r_re <= {CB{1'b0}}; m_r_im <= {CB{1'b0}};",
             "pre pipe reset")

# ----------------------------------------------------------------------
# w8_post: rotate the DSP PRODUCT (PREG output) into the existing shift
# stage.  Product is Q(td), q adds td more fractional bits -> the fused
# stage shift grows by td (2*td + s).  Applied to ALL products (worst
# case duty).
# ----------------------------------------------------------------------
POST = add_q8_param(BASE)
POST = patch(
    POST,
    "    reg signed [MWB-1:0] p_re, p_im;",
    """    reg signed [MWB-1:0] p_re, p_im;

    // ---- S7 W8 probe: product-side 45-degree fabric rotation ----
    // (p_re + j p_im) * (q/2^td)(1 - j): re' = (p_re+p_im)*q,
    // im' = (p_im-p_re)*q -- exact integer arithmetic, Q(2*td).
    localparam integer PW45 = MWB + 1 + TWIDDLE_WIDTH;
    function signed [PW45-1:0] mul_q8p;
        input signed [PW45-1:0] v;
        integer b;
        reg signed [PW45-1:0] acc;
        begin
            acc = {PW45{1'b0}};
            for (b = 0; b < TWIDDLE_WIDTH; b = b + 1)
                if (Q8[b]) acc = acc + (v <<< b);
            mul_q8p = acc;
        end
    endfunction
    wire signed [PW45-1:0] p45a_in = $signed({{1{p_re[MWB-1]}}, p_re})
                                   + $signed({{1{p_im[MWB-1]}}, p_im});
    wire signed [PW45-1:0] p45b_in = $signed({{1{p_im[MWB-1]}}, p_im})
                                   - $signed({{1{p_re[MWB-1]}}, p_re});
    (* use_dsp = "no" *) wire signed [PW45-1:0] p45a_q = mul_q8p(p45a_in);
    (* use_dsp = "no" *) wire signed [PW45-1:0] p45b_q = mul_q8p(p45b_in);
    // the fused stage shift grows by td; result fits PW after the shift
    localparam integer TD45_S1   = 2 * TWIDDLE_DECIMAL + SIGMA1;
    localparam integer TD45_BOTH = 2 * TWIDDLE_DECIMAL + SIGMA0 + SIGMA1;
    function signed [PW45-1:0] round_shift_p45;
        input signed [PW45-1:0] v;
        input integer sh;
        begin
            if (sh <= 0)
                round_shift_p45 = v;
            else
                round_shift_p45 = (v + ($signed({{(PW45-1){1'b0}}, 1'b1})
                                       <<< (sh-1))) >>> sh;
        end
    endfunction""",
    "post block")
POST = patch(
    POST,
    """            if (k7 >= THREE_D) begin
                shift_p_re <= round_shift_pw({{1{p_re[MWB-1]}}, p_re}, TD_PLUS_S1);
                shift_p_im <= round_shift_pw({{1{p_im[MWB-1]}}, p_im}, TD_PLUS_S1);
            end else begin
                shift_p_re <= round_shift_pw({{1{p_re[MWB-1]}}, p_re}, TD_PLUS_BOTH);
                shift_p_im <= round_shift_pw({{1{p_im[MWB-1]}}, p_im}, TD_PLUS_BOTH);
            end""",
    """            if (k7 >= THREE_D) begin
                shift_p_re <= round_shift_p45(p45a_q, TD45_S1);
                shift_p_im <= round_shift_p45(p45b_q, TD45_S1);
            end else begin
                shift_p_re <= round_shift_p45(p45a_q, TD45_BOTH);
                shift_p_im <= round_shift_p45(p45b_q, TD45_BOTH);
            end""",
    "post shift")

# w8_post_pipe: the tap tree's OUTPUT gets its own register; the fused
# round+slice moves to the next hop (latency +1, k7 select unchanged).
POSTP = patch(
    POST,
    "    function signed [PW45-1:0] round_shift_p45;",
    "    reg signed [PW45-1:0] p45q_re, p45q_im; // S7 probe: tree output reg\n"
    "    function signed [PW45-1:0] round_shift_p45;",
    "post pipe decl")
POSTP = patch(
    POSTP,
    """            if (k7 >= THREE_D) begin
                shift_p_re <= round_shift_p45(p45a_q, TD45_S1);
                shift_p_im <= round_shift_p45(p45b_q, TD45_S1);
            end else begin
                shift_p_re <= round_shift_p45(p45a_q, TD45_BOTH);
                shift_p_im <= round_shift_p45(p45b_q, TD45_BOTH);
            end""",
    """            p45q_re <= p45a_q;
            p45q_im <= p45b_q;
            if (k7 >= THREE_D) begin
                shift_p_re <= round_shift_p45(p45q_re, TD45_S1);
                shift_p_im <= round_shift_p45(p45q_im, TD45_S1);
            end else begin
                shift_p_re <= round_shift_p45(p45q_re, TD45_BOTH);
                shift_p_im <= round_shift_p45(p45q_im, TD45_BOTH);
            end""",
    "post pipe shift")
POSTP = patch(POSTP,
              "            p_re <= {MWB{1'b0}}; p_im <= {MWB{1'b0}};",
              "            p_re <= {MWB{1'b0}}; p_im <= {MWB{1'b0}};\n"
              "            p45q_re <= {PW45{1'b0}}; p45q_im <= {PW45{1'b0}};",
              "post pipe reset")

VARIANTS = {"fft_stage_base.v": PRE if False else add_q8_param(BASE),
            "fft_stage_w8_pre.v": PRE,
            "fft_stage_w8_pre_pipe.v": PREP,
            "fft_stage_w8_post.v": POST,
            "fft_stage_w8_post_pipe.v": POSTP}

if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "variants"), exist_ok=True)
    for name, text in VARIANTS.items():
        path = os.path.join(HERE, "variants", name)
        open(path, "w").write(text)
        print(f"wrote {path} ({len(text.splitlines())} lines)")
