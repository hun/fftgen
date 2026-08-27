"""R2^2 top generator: pair chain + odd-n leftover + output quantizer."""


def _leftover_preload(cfg):
    """Post-warm leftover state from the model (mirrors the plain model's
    stage_preloads fields)."""
    from golden import R22SDFGoldenModel
    m = R22SDFGoldenModel(cfg)
    pl = m.leftover_preload
    pipe = int("".join("1" if b else "0"
                       for b in reversed(pl["pipe"][:9])), 2)
    return {"wptr": pl["wptr"], "pwp": pl["pwp"], "raddr": pl["raddr"],
            "pipe": pipe, "phase_i": pl["phase_i"],
            "compute": pl["compute"]}


def top_rtl(cfg):
    """Generate the R2^2 top (all N: pairs + odd-n leftover + quantizer)."""
    N = cfg.num_points
    n = cfg.num_stages
    npairs = n // 2
    leftover = (n % 2 == 1)
    intern = cfg.sample_width + max(0, n - sum(cfg.shifts)) + 1

    se_re = ('    wire signed [INTERN_WIDTH-1:0] in_x_re =\n'
             '        {{(INTERN_WIDTH-SAMPLE_WIDTH){in_re[SAMPLE_WIDTH-1]}}, '
             'in_re};')
    se_im = ('    wire signed [INTERN_WIDTH-1:0] in_x_im =\n'
             '        {{(INTERN_WIDTH-SAMPLE_WIDTH){in_im[SAMPLE_WIDTH-1]}}, '
             'in_im};')

    stages = []
    for g in range(npairs):
        D = N >> (2 * g + 2)
        sig0 = cfg.shifts[2 * g]
        sig1 = cfg.shifts[2 * g + 1]
        rom_base = sum(3 * (N >> (2 * t + 2)) for t in range(g))
        up_lat = sum(3 * (N >> (2 * t + 2)) + 1 for t in range(g))
        k_pre = (-up_lat) % (4 * D)
        stages.append(f"""    fft_stage_r22 #(
        .DEPTH          ({D}),
        .WIDTH          (INTERN_WIDTH),
        .SIGMA0         ({sig0}),
        .SIGMA1         ({sig1}),
        .TWIDDLE_WIDTH  ({cfg.twiddle_width}),
        .TWIDDLE_DECIMAL({cfg.twiddle_decimal}),
        .ROM_BASE       ({rom_base}),
        .NPTS           ({N}),
        .INVERSE        ({1 if cfg.inverse else 0}),
        .K_PRELOAD      ({k_pre}),
        .TWIDDLE_FILE   (TWIDDLE_FILE)
    ) u_stage_{g} (
        .clk(clk), .ce(ce), .rst(rst),
        .in_re  ({'in_x_re' if g == 0 else f'w_re[{g-1}]'}),
        .in_im  ({'in_x_im' if g == 0 else f'w_im[{g-1}]'}),
        .out_re (w_re[{g}]), .out_im (w_im[{g}])
    );""")

    lo = ""
    nout = "w_re[NPAIRS-1]"
    nout_im = "w_im[NPAIRS-1]"
    if leftover:
        lo_pre = _leftover_preload(cfg)
        lo = f"""    fft_stage #(
        .DEPTH          (1),
        .WIDTH          (INTERN_WIDTH),
        .SHIFT          ({cfg.shifts[n-1]}),
        .TWIDDLE_WIDTH  ({cfg.twiddle_width}),
        .TWIDDLE_DECIMAL({cfg.twiddle_decimal}),
        .K_STRIDE       ({1 << (n - 1)}),
        .ROM_BASE       ({sum(3 * (N >> (2 * t + 2)) for t in range(npairs))}),
        .NPTS           ({N}),
        .PRELOAD_I      ({lo_pre['phase_i']}),
        .PRELOAD_C      ({1 if lo_pre['compute'] else 0}),
        .WPTR_PRE       ({lo_pre['wptr']}),
        .PWP_PRE        ({lo_pre['pwp']}),
        .RADDR_PRE      ({lo_pre['raddr']}),
        .PIPE_PRE       ({lo_pre['pipe']}),
        .TOPOLOGY       (0),
        .TRIVIAL        (1),
        .TWIDDLE_MEM    (1),
        .TWIDDLE_FILE   (TWIDDLE_FILE)
    ) u_leftover (
        .clk(clk), .ce(ce), .rst(rst),
        .in_re  (w_re[NPAIRS-1]), .in_im (w_im[NPAIRS-1]),
        .out_re (w_lo_re), .out_im (w_lo_im)
    );"""
        nout = "w_lo_re"
        nout_im = "w_lo_im"

    quant = '''    localparam integer QW = INTERN_WIDTH + 8 + OUTPUT_WIDTH + 2;
    function [OUTPUT_WIDTH-1:0] quant_out;
        input signed [INTERN_WIDTH-1:0] v;
        reg signed [QW-1:0] t;
        reg signed [QW-1:0] hi, lo_q;
        reg [OUTPUT_WIDTH-1:0] ohi, olo;
        begin
            ohi = {1'b0, {(OUTPUT_WIDTH-1){1'b1}}};
            olo = {1'b1, {(OUTPUT_WIDTH-1){1'b0}}};
            hi  = {{(QW-OUTPUT_WIDTH){1'b0}}, ohi};
            lo_q = {{(QW-OUTPUT_WIDTH){1'b1}}, olo};
            if (SAMPLE_DECIMAL > OUTPUT_DECIMAL)
                t = ($signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}}, v})
                     + ($signed({{(QW-1){1'b0}}, 1'b1})
                        <<< (SAMPLE_DECIMAL - OUTPUT_DECIMAL - 1)))
                    >>> (SAMPLE_DECIMAL - OUTPUT_DECIMAL);
            else if (SAMPLE_DECIMAL < OUTPUT_DECIMAL)
                t = $signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}}, v})
                    <<< (OUTPUT_DECIMAL - SAMPLE_DECIMAL);
            else
                t = $signed({{(QW-INTERN_WIDTH){v[INTERN_WIDTH-1]}}, v});
            if (t > hi) quant_out = ohi;
            else if (t < lo_q) quant_out = olo;
            else quant_out = t[OUTPUT_WIDTH-1:0];
        end
    endfunction
'''

    src = f"""// spike-generated R2^2 top (all N)
`default_nettype none
module fft_r22_top #(
    parameter integer NUM_POINTS = {N},
    parameter integer SAMPLE_WIDTH = {cfg.sample_width},
    parameter integer SAMPLE_DECIMAL = {cfg.sample_decimal},
    parameter integer OUTPUT_WIDTH = {cfg.output_width},
    parameter integer OUTPUT_DECIMAL = {cfg.output_decimal},
    parameter integer TWIDDLE_WIDTH = {cfg.twiddle_width},
    parameter integer TWIDDLE_DECIMAL = {cfg.twiddle_decimal},
    parameter integer INVERSE = {1 if cfg.inverse else 0},
    parameter integer INTERN_WIDTH = {intern},
    parameter TWIDDLE_FILE = "fft_twiddles_r22.mem"
)(
    input wire clk, ce, rst,
    input wire signed [SAMPLE_WIDTH-1:0] in_re, in_im,
    output wire signed [OUTPUT_WIDTH-1:0] out_re, out_im
);
    localparam integer NPAIRS = {npairs};
    wire signed [INTERN_WIDTH-1:0] w_re [0:NPAIRS-1];
    wire signed [INTERN_WIDTH-1:0] w_im [0:NPAIRS-1];
    wire signed [INTERN_WIDTH-1:0] w_lo_re, w_lo_im;
{se_re}
{se_im}
{chr(10).join(stages)}
{lo}
    {quant}
    assign out_re = quant_out({nout});
    assign out_im = quant_out({nout_im});
endmodule
`default_nettype wire
"""
    lat = sum(3 * (N >> (2 * t + 2)) + 1 for t in range(npairs))
    if leftover:
        lat += 11
    return src, lat
