"""Spike S5e: DIT R2^2 stage RTL bit-exact verification (P7).

Writes the DIT-order twiddle ROM (pairs A = lo, lo+2, ... in DIT order
with slices [T[2j*4^m']], [T[j*4^m']], [T[3j*4^m']]), builds the DIT
chain (odd-n leftover stage 0 FIRST as a plain DIT fft_stage, then the
fft_stage_r22_dit pairs) + output quantizer, drives Verilator, and
compares bit-exactly against R22SDFGoldenModelDit.
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "src")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from golden import R22SDFGoldenModelDit
from twiddles import canonical_twiddles
from typing import Any, Dict

SPIKE = os.path.dirname(os.path.abspath(__file__))
RTL = os.path.join(SPIKE, "..", "..", "rtl")


def bitrev_order(N, n):
    return [int(format(k, f"0{n}b")[::-1], 2) for k in range(N)]


def write_r22_dit_twiddle_mem(cfg, path):
    """DIT-order ROM: pair A (group depth H=2^A, stride 4^{m'}) with
    slices [T[2j*4^m']], [T[j*4^m']], [T[3j*4^m']] for j in [0, H)."""
    N = cfg.num_points
    n = cfg.num_stages
    tw = canonical_twiddles(N, cfg.twiddle_width, cfg.twiddle_decimal,
                            cfg.inverse)
    words = []
    lo = 0 if n % 2 == 0 else 1
    k = 0
    while lo + 2 * k + 1 < n:
        A = lo + 2 * k
        H = 1 << A
        base = 4 ** ((n - 2 - A) // 2)
        for which in (2, 1, 3):          # slice order: t1, t2, t3
            for j in range(H):
                re, im = tw[(which * j * base) % N]
                words.append(((re & ((1 << cfg.twiddle_width) - 1))
                              << cfg.twiddle_width)
                             | (im & ((1 << cfg.twiddle_width) - 1)))
        k += 1
    if n % 2 == 1:
        re, im = tw[0]                   # leftover stage 0: W^0
        words.append(((re & ((1 << cfg.twiddle_width) - 1))
                      << cfg.twiddle_width)
                     | (im & ((1 << cfg.twiddle_width) - 1)))
    with open(path, "w") as f:
        for w in words:
            f.write("%0*x\n" % ((cfg.twiddle_width * 2 + 3) // 4, w))


def top_dit_rtl(cfg):
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
    rom_bases = []
    lo = 0 if n % 2 == 0 else 1
    up_lat = 11 if leftover else 0
    k = 0
    while lo + 2 * k + 1 < n:
        A = lo + 2 * k
        H = 1 << A
        base = 4 ** ((n - 2 - A) // 2)
        rom_base = sum(3 * (1 << (lo + 2 * t)) for t in range(k))
        rom_bases.append(rom_base)
        k_pre = (-up_lat) % (4 * H)
        stages.append(f"""    fft_stage_r22_dit #(
        .DEPTH          ({H}),
        .WIDTH          (INTERN_WIDTH),
        .SIGMA0         ({cfg.shifts[A]}),
        .SIGMA1         ({cfg.shifts[A + 1]}),
        .TWIDDLE_WIDTH  ({cfg.twiddle_width}),
        .TWIDDLE_DECIMAL({cfg.twiddle_decimal}),
        .ROM_BASE       ({rom_base}),
        .NPTS           ({N}),
        .INVERSE        ({1 if cfg.inverse else 0}),
        .K_PRELOAD      ({k_pre}),
        .TWIDDLE_FILE   (TWIDDLE_FILE)
    ) u_stage_{k} (
        .clk(clk), .ce(ce), .rst(rst),
        .in_re  ({'w_lo_re' if (k == 0 and leftover) else
                  ('in_x_re' if k == 0 else f'w_re[{k-1}]')}),
        .in_im  ({'w_lo_im' if (k == 0 and leftover) else
                  ('in_x_im' if k == 0 else f'w_im[{k-1}]')}),
        .out_re (w_re[{k}]), .out_im (w_im[{k}])
    );""")
        up_lat += 3 * H + 1
        k += 1
    # leftover stage 0 (odd n): plain DIT fft_stage, trivial +/-1
    lo_inst = ""
    nout = "w_re[NPAIRS-1]"
    nout_im = "w_im[NPAIRS-1]"
    if leftover:
        lo_inst = f"""    fft_stage #(
        .DEPTH          (1),
        .WIDTH          (INTERN_WIDTH),
        .SHIFT          ({cfg.shifts[0]}),
        .TWIDDLE_WIDTH  ({cfg.twiddle_width}),
        .TWIDDLE_DECIMAL({cfg.twiddle_decimal}),
        .K_STRIDE       ({1 << (n - 1)}),
        .ROM_BASE       ({sum(3 * (1 << (lo + 2 * t)) for t in range(npairs))}),
        .NPTS           ({N}),
        .PRELOAD_I      (0),
        .PRELOAD_C      (0),
        .WPTR_PRE       (0),
        .PWP_PRE        (0),
        .RADDR_PRE      (0),
        .PIPE_PRE       (0),
        .TOPOLOGY       (1),
        .TRIVIAL        (1),
        .TWIDDLE_MEM    (1),
        .TWIDDLE_FILE   (TWIDDLE_FILE)
    ) u_leftover (
        .clk(clk), .ce(ce), .rst(rst),
        .in_re  (in_x_re), .in_im (in_x_im),
        .out_re (w_lo_re), .out_im (w_lo_im)
    );"""
        nout = "w_re[NPAIRS-1]"
        nout_im = "w_im[NPAIRS-1]"
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
    src = f"""// spike-generated R2^2 DIT top (all N)
`default_nettype none
module fft_r22_dit_top #(
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
{lo_inst}
{chr(10).join(stages)}
    {quant}
    assign out_re = quant_out({nout});
    assign out_im = quant_out({nout_im});
endmodule
`default_nettype wire
"""
    lat = (11 if leftover else 0) + sum(3 * (1 << (lo + 2 * t)) + 1
                                        for t in range(npairs))
    return src, lat


DRIVER = r"""
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <string>
#include <vector>
#include "Vfft_r22_dit_top.h"
#include "verilated.h"

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
#ifndef TB_SAMPLE_WIDTH
#define TB_SAMPLE_WIDTH 16
#endif
#ifndef TB_OUTPUT_WIDTH
#define TB_OUTPUT_WIDTH 16
#endif
    int OW = TB_OUTPUT_WIDTH;

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

    Vfft_r22_dit_top* dut = new Vfft_r22_dit_top;
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
        int64_t ore = dut->out_re;
        int64_t oim = dut->out_im;
        int64_t mre = (ore & ((int64_t)1 << (OW-1))) ? (ore | ~(((int64_t)1 << OW)-1)) : ore;
        int64_t mim = (oim & ((int64_t)1 << (OW-1))) ? (oim | ~(((int64_t)1 << OW)-1)) : oim;
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


def run_rtl(cfg, num_frames=2, seed=7) -> Dict[str, Any]:
    import random
    N = cfg.num_points
    outdir = os.path.join(SPIKE, "build_r22_dit")
    os.makedirs(outdir, exist_ok=True)
    shutil.copy(os.path.join(RTL, "fft_stage_r22_dit.v"), outdir)
    shutil.copy(os.path.join(RTL, "fft_sdf.v"), outdir)
    write_r22_dit_twiddle_mem(cfg, os.path.join(outdir, "fft_twiddles_r22.mem"))
    top, lat = top_dit_rtl(cfg)
    open(os.path.join(outdir, "fft_r22_dit_top.v"), "w").write(top)
    open(os.path.join(outdir, "tb_driver.cpp"), "w").write(DRIVER)

    br = bitrev_order(N, cfg.num_stages)
    rng = random.Random(seed)
    hi = 2 ** (cfg.sample_width - 1)
    raw = [(rng.randint(-hi, hi - 1), rng.randint(-hi, hi - 1))
           for _ in range(num_frames * N)]
    samples = [raw[f * N + br[j]] for f in range(num_frames) for j in range(N)]
    with open(os.path.join(outdir, "stimulus.txt"), "w") as f:
        for r, i in samples:
            f.write("%x %x\n" % (r & ((1 << cfg.sample_width) - 1),
                                 i & ((1 << cfg.sample_width) - 1)))

    expq = R22SDFGoldenModelDit(cfg).process_stream(samples)
    intern = cfg.sample_width + max(0, cfg.num_stages
                                    - sum(cfg.shifts)) + 1
    gargs = [
        "-GNUM_POINTS=%d" % N,
        "-GSAMPLE_WIDTH=%d" % cfg.sample_width,
        "-GSAMPLE_DECIMAL=%d" % cfg.sample_decimal,
        "-GOUTPUT_WIDTH=%d" % cfg.output_width,
        "-GOUTPUT_DECIMAL=%d" % cfg.output_decimal,
        "-GTWIDDLE_WIDTH=%d" % cfg.twiddle_width,
        "-GTWIDDLE_DECIMAL=%d" % cfg.twiddle_decimal,
        "-GINVERSE=%d" % (1 if cfg.inverse else 0),
        "-GINTERN_WIDTH=%d" % intern,
    ]
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", "fft_r22_dit_top", "-Wno-fatal",
           "-CFLAGS", "-DTB_SAMPLE_WIDTH=%d -DTB_OUTPUT_WIDTH=%d -DTB_CYCLES=%d"
           % (cfg.sample_width, cfg.output_width, len(samples) + lat + 4),
           *gargs,
           "fft_r22_dit_top.v", "fft_stage_r22_dit.v", "fft_sdf.v",
           "tb_driver.cpp"]
    import shutil as _sh
    _sh.rmtree(os.path.join(outdir, "obj_dir"), ignore_errors=True)
    r = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-2000:]}
    r = subprocess.run([os.path.join("obj_dir", "Vfft_r22_dit_top")],
                       cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        return {"rc": r.returncode, "log": r.stderr[-2000:]}

    act = [tuple(int(x) for x in l.split())
           for l in open(os.path.join(outdir, "actual.txt")) if l.strip()]
    for skip in range(0, lat + 8):
        tail = act[skip:]
        n_cmp = min(len(expq), len(tail))
        if n_cmp < N:
            break
        if all(tail[i] == expq[i] for i in range(n_cmp)):
            return {"rc": 0, "skip": skip, "lat": lat,
                    "n": n_cmp, "len_act": len(act)}
    return {"rc": 1, "lat": lat, "log": "no alignment found",
            "first_act": act[:6], "first_exp": expq[:6]}


if __name__ == "__main__":
    from config import FFTConfig
    for N in (4, 8, 16, 32, 64, 128):
        for inv in (False, True):
            cfg = FFTConfig(num_points=N, inverse=inv,
                            input_order="bitreversed",
                            output_order="native")
            res = run_rtl(cfg)
            if res["rc"] == 0:
                print(f"N={N:3d} inv={int(inv)}: BIT-EXACT "
                      f"(skip={res['skip']}, lat={res['lat']}, n={res['n']})")
            else:
                print(f"N={N:3d} inv={int(inv)}: FAIL "
                      f"{res.get('log','')[:40]}")
                print("  act:", res.get("first_act"))
                print("  exp:", res.get("first_exp"))
