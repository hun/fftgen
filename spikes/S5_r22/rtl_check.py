"""Spike S5c: R2^2 stage RTL bit-exact verification (P7).

Writes the R2^2 twiddle ROM (per-pair stride-4^m slices), builds the
stage chain (fft_r22_top.v) with Verilator, drives stimulus, and
compares the captured stream bit-exactly against the re-pinned
contract (R22SDFGoldenModel / fft_fixed_batch_r22).
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "src")

from config import FFTConfig
from golden import R22SDFGoldenModel
from twiddles import canonical_twiddles

SPIKE = os.path.dirname(os.path.abspath(__file__))
RTL = os.path.join(SPIKE, "..", "..", "rtl")


def write_r22_twiddle_mem(cfg, path):
    """R2^2 twiddle ROM: pair m occupies [BASE_m, BASE_m + 3*D_m) with
    slices [T[g*4^m]], [T[2g*4^m]], [T[3g*4^m]] for g in [0, D_m)."""
    N = cfg.num_points
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    words = []
    m = 0
    while 2 * m + 1 < cfg.num_stages:
        D = N >> (2 * m + 2)
        base = 4 ** m
        for which in (1, 2, 3):          # slice order: y1, y2, y3
            for g in range(D):
                re, im = tw[(which * g * base) % N]
                words.append(((re & ((1 << cfg.twiddle_width) - 1))
                              << cfg.twiddle_width)
                             | (im & ((1 << cfg.twiddle_width) - 1)))
        m += 1
    with open(path, "w") as f:
        for w in words:
            f.write("%0*x\n" % ((cfg.twiddle_width * 2 + 3) // 4, w))


def top_rtl(cfg):
    """Generate the R2^2 chain top (even stage counts only for now)."""
    N = cfg.num_points
    n = cfg.num_stages
    assert n % 2 == 0, "leftover-stage chain not implemented yet"
    npairs = n // 2
    # sign-extension of the 16-bit input into the intern width (plain
    # strings -- no f-string brace escaping)
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
    src = f"""// spike-generated R2^2 chain top (even n)`default_nettype none
module fft_r22_top #(
    parameter integer NUM_POINTS = {N},
    parameter integer SAMPLE_WIDTH = {cfg.sample_width},
    parameter integer TWIDDLE_WIDTH = {cfg.twiddle_width},
    parameter integer TWIDDLE_DECIMAL = {cfg.twiddle_decimal},
    parameter integer INVERSE = {1 if cfg.inverse else 0},
    parameter integer INTERN_WIDTH = {cfg.sample_width + 5},
    parameter TWIDDLE_FILE = "fft_twiddles_r22.mem"
)(
    input wire clk, ce, rst,
    input wire signed [SAMPLE_WIDTH-1:0] in_re, in_im,
    output wire signed [INTERN_WIDTH-1:0] out_re, out_im
);
    localparam integer NP = NUM_POINTS;
    localparam integer NPAIRS = {npairs};
    localparam integer NSTAGES = {n};
    localparam integer LATENCY = {sum(3 * (N >> (2 * t + 2)) + 1 for t in range(npairs))};
    wire signed [INTERN_WIDTH-1:0] w_re [0:NPAIRS-1];
    wire signed [INTERN_WIDTH-1:0] w_im [0:NPAIRS-1];
{se_re}
{se_im}
{chr(10).join(stages)}
    assign out_re = w_re[NPAIRS-1];
    assign out_im = w_im[NPAIRS-1];
endmodule
`default_nettype wire
"""
    return src, sum(3 * (N >> (2 * t + 2)) + 1 for t in range(npairs))


DRIVER = r"""
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <string>
#include <vector>
#include "Vfft_r22_top.h"
#include "verilated.h"

static int64_t parse_hex(const std::string& s, int width) {
    uint64_t v = strtoull(s.c_str(), nullptr, 16);
    int64_t sign = (int64_t)1 << (width - 1);
    int64_t r = v & (((int64_t)1 << width) - 1);
    if (r & sign) r -= ((int64_t)1 << width);
    return r;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
#ifndef TB_SAMPLE_WIDTH
#define TB_SAMPLE_WIDTH 16
#endif
    int SW = TB_SAMPLE_WIDTH;
    int IW = TB_INTERN_WIDTH;

    std::ifstream fstim("stimulus.txt");
    std::vector<int64_t> stim_re, stim_im;
    std::string line;
    while (std::getline(fstim, line)) {
        if (line.empty() || line[0] == '#') continue;
        long long a, b;
        if (sscanf(line.c_str(), "%llx %llx", &a, &b) != 2) continue;
        stim_re.push_back(a); stim_im.push_back(b);
    }
    size_t T = stim_re.size();
    if (T == 0) { fprintf(stderr, "empty stimulus\n"); return 2; }

    Vfft_r22_top* dut = new Vfft_r22_top;
    dut->rst = 1; dut->ce = 0;
    dut->in_re = 0; dut->in_im = 0;
    dut->clk = 0;
    for (int i = 0; i < 4; i++) {
        dut->clk = !dut->clk; dut->eval();
        dut->clk = !dut->clk; dut->eval();
    }
    dut->rst = 0;

    std::ofstream fout("actual.txt");
    uint64_t cycle = 0;
    while (cycle < TB_CYCLES) {
        bool feeding = (cycle < T);
        int64_t r = feeding ? stim_re[cycle] : 0;
        int64_t im = feeding ? stim_im[cycle] : 0;
        dut->in_re = r; dut->in_im = im;
        dut->ce = 1;
        dut->clk = 1; dut->eval();
        // sign-extend intern width for printing
        int64_t ore = dut->out_re;
        int64_t oim = dut->out_im;
        int64_t mre = (ore & ((int64_t)1 << (IW-1))) ? (ore | ~(((int64_t)1 << IW)-1)) : ore;
        int64_t mim = (oim & ((int64_t)1 << (IW-1))) ? (oim | ~(((int64_t)1 << IW)-1)) : oim;
        fout << mre << " " << mim << "\n";
        dut->clk = 0; dut->eval();
        cycle++;
    }
    fout.close();
    delete dut;
    fprintf(stderr, "done: %zu samples fed\n", T);
    return 0;
}
"""


def run_rtl(cfg, num_frames=2, seed=7):
    import random
    from quant import quantize_output
    N = cfg.num_points
    outdir = os.path.join(SPIKE, "build_r22_rtl")
    os.makedirs(outdir, exist_ok=True)
    shutil.copy(os.path.join(RTL, "fft_stage_r22.v"), outdir)

    write_r22_twiddle_mem(cfg, os.path.join(outdir, "fft_twiddles_r22.mem"))
    top, lat = top_rtl(cfg)
    open(os.path.join(outdir, "fft_r22_top.v"), "w").write(top)
    open(os.path.join(outdir, "tb_driver.cpp"), "w").write(DRIVER)

    # stimulus
    rng = random.Random(seed)
    hi = 2 ** (cfg.sample_width - 1)
    samples = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
               for _ in range(num_frames * N)]
    with open(os.path.join(outdir, "stimulus.txt"), "w") as f:
        for r, i in samples:
            f.write("%x %x\n" % (r & ((1 << cfg.sample_width) - 1),
                                 i & ((1 << cfg.sample_width) - 1)))

    # expected (contract): the model's raw intern-width stream, then the
    # final output quantization (the full core applies it at the top)
    m = R22SDFGoldenModel(cfg)
    intern = cfg.sample_width + max(0, cfg.num_stages
                                    - sum(cfg.shifts)) + 1
    expq = []
    for f in range(num_frames):
        fr = samples[f * N:(f + 1) * N]
        raw = []
        T = len(fr)
        for pos in range(T + m.latency):
            src = fr[pos] if pos < T else (0, 0)
            cur = src[:2]
            up = 0
            for st in m.stages:
                cur = st.step(tuple(cur), pos - up)
                up += st.latency
            raw.append(cur)
        raw = raw[m.latency:]
        for re, im in m._apply_leftover(raw):
            expq.append(quantize_output(re, im, cfg.sample_decimal,
                                        cfg.output_width,
                                        cfg.output_decimal))
    gargs = [
        "-GNUM_POINTS=%d" % N,
        "-GSAMPLE_WIDTH=%d" % cfg.sample_width,
        "-GTWIDDLE_WIDTH=%d" % cfg.twiddle_width,
        "-GTWIDDLE_DECIMAL=%d" % cfg.twiddle_decimal,
        "-GINVERSE=%d" % (1 if cfg.inverse else 0),
        "-GINTERN_WIDTH=%d" % intern,
    ]
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", "fft_r22_top", "-Wno-fatal",
           "-CFLAGS", "-DTB_SAMPLE_WIDTH=%d -DTB_INTERN_WIDTH=%d -DTB_CYCLES=%d"
           % (cfg.sample_width, intern, len(samples) + lat + 4),
           *gargs,
           "fft_r22_top.v", "fft_stage_r22.v", "tb_driver.cpp"]
    import shutil as _sh
    _sh.rmtree(os.path.join(outdir, "obj_dir"), ignore_errors=True)
    r = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-2000:]}
    r = subprocess.run([os.path.join("obj_dir", "Vfft_r22_top")],
                       cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-2000:]}

    act = [tuple(int(x) for x in l.split())
           for l in open(os.path.join(outdir, "actual.txt")) if l.strip()]
    # the RTL emits the unquantized intern-width stream; apply the final
    # quantization (the core's top does this) before comparing
    actq = [quantize_output(a[0], a[1], cfg.sample_decimal,
                            cfg.output_width, cfg.output_decimal)
            for a in act]
    for skip in range(0, lat + 8):
        tail = actq[skip:]
        n_cmp = min(len(expq), len(tail))
        if n_cmp < N:
            break
        if all(tail[i] == expq[i] for i in range(n_cmp)):
            return {"rc": 0, "skip": skip, "lat": lat,
                    "n": n_cmp, "len_act": len(act)}
    return {"rc": 1, "lat": lat, "log": "no alignment found",
            "first_act": act[:6], "first_exp": expq[:6]}


if __name__ == "__main__":
    for N in (4, 16, 64):
        for inv in (False, True):
            cfg = FFTConfig(num_points=N, inverse=inv)
            res = run_rtl(cfg)
            if res["rc"] == 0:
                print(f"N={N:3d} inv={int(inv)}: BIT-EXACT "
                      f"(skip={res['skip']}, lat={res['lat']}, n={res['n']})")
            else:
                print(f"N={N:3d} inv={int(inv)}: FAIL {res.get('log','')}")
                print("  act head:", res.get("first_act"))
                print("  exp head:", res.get("first_exp"))
