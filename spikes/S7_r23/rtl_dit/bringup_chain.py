#!/usr/bin/env python3
"""Chain bring-up for fft_sdf_r23_dit (S8): bit-exact vs
golden.R23SDFGoldenModelDit (the r23 IFFT, bitrev -> natural).

Feeds two frames of a random bit-reversed spectrum through the RTL
wrapper and compares the first output frame against the golden model.
"""
import math
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from config import FFTConfig
from golden import R23SDFGoldenModelDit
from twiddles import canonical_twiddles

TD, TW, W = 17, 18, 16


def q8_of(td):
    return int(math.floor(math.sqrt(2) / 2 * (1 << td) + 0.5))


def bitrev(p, bits):
    v = 0
    for _ in range(bits):
        v = (v << 1) | (p & 1)
        p >>= 1
    return v


def run(N, seed=7, verbose=True, kp0=-1, kp1=-1, kp2=-1):
    cfg = FFTConfig(num_points=N, input_order="bitreversed",
                    output_order="native", inverse=True)
    model = R23SDFGoldenModelDit(cfg)
    NS = N.bit_length() - 1
    MMAX, R = NS // 3, NS % 3
    bits = NS

    rng = random.Random(seed + N)
    Xnat = [(rng.randint(-900, 900), rng.randint(-900, 900)) for _ in range(N)]
    stream = [(Xnat[bitrev(p, bits)][0], Xnat[bitrev(p, bits)][1])
              for p in range(N)]
    exp = model.process_stream(stream * 2)[:N]      # frame 0

    tw = canonical_twiddles(N, TW, TD, True)
    br3 = [0, 4, 2, 6, 1, 5, 3, 7]

    # ---- twiddle file: leftover slices + window-ordered triples ----
    Gs = [N >> (3 * (MMAX - 1 - j) + 3) for j in range(MMAX)]
    bases = []
    acc = (2 ** R - 1) if R else 0
    words = {}
    for s in range(R):
        D = 1 << s
        for j in range(D):
            words[(2 ** s - 1) + j] = tw[(j * (N >> (s + 1))) % N]
    for j in range(MMAX):
        m = MMAX - 1 - j
        bases.append(acc)
        for w in range(1, 8):
            kk = br3[w]
            for g in range(Gs[j]):
                words[acc + (w - 1) * Gs[j] + g] = \
                    tw[(kk * g * (8 ** m)) % N]
        acc += 7 * Gs[j]
    twf = os.path.join(HERE, f"tw_chain_{N}.mem")
    with open(twf, "w") as f:
        for idx in range(N):               # pad to NPTS (the ROM word count)
            re, im = words.get(idx, (0, 0))
            f.write(f"{(re & (2**TW-1)) << TW | (im & (2**TW-1)):x}\n")

    # ---- stimulus ----
    stim = os.path.join(HERE, f"stim_chain_{N}.txt")
    with open(stim, "w") as f:
        for re, im in stream * 2:
            f.write(f"{(re & (2**W-1)) << W | (im & (2**W-1)):x}\n")

    # the golden's second r2 leftover post-warm state (for R >= 2)
    r2b = dict(i=0xFFFF, c=0, wptr=0xFFFF, pwp=0xFFFF, raddr=0xFFFF, pipe=0)
    if R >= 2:
        st1 = model.r2[1]
        pipe = 0
        for k in range(9):
            pipe |= (1 << k) if st1.pipe_comp[k] else 0
        r2b = dict(i=st1.i, c=int(st1.in_compute), wptr=st1.wptr,
                   pwp=st1.pwp, raddr=st1.raddr, pipe=pipe)
    LAT = model.latency          # RTL = golden here (verified per stage)
    tbv = os.path.join(HERE, "tb_chain_dit.v")
    with open(tbv, "w") as f:
        f.write(f"""`timescale 1ns/1ps
module tb;
    localparam integer W = {W};
    localparam integer N = {N};
    localparam integer LAT = {LAT};
    localparam integer TOTAL = 2*N;
    reg clk = 0, rst = 1;
    reg tvalid = 1, tuser = 0, tlast = 0;
    reg [2*W-1:0] stim [0:TOTAL-1];
    integer c;
    integer fd;
    wire mvalid;
    wire signed [W-1:0] ore, oim;
    wire muser, mlast;
    fft_sdf_r23_dit #(
        .NUM_POINTS({N}), .SAMPLE_WIDTH({W}), .INTERN_WIDTH({W}),
        .TWIDDLE_WIDTH({TW}), .TWIDDLE_DECIMAL({TD}),
        .SCALING_PACK(64'h{((1 << (2 * NS)) - 1) // 3:x}),
        .INVERSE(1), .TWIDDLE_FILE("{twf}"), .Q8({q8_of(TD)}),
        .R2B_I(16'h{r2b['i']:x}), .R2B_C(1'h{r2b['c']}),
        .R2B_WPTR(16'h{r2b['wptr']:x}), .R2B_PWP(16'h{r2b['pwp']:x}),
        .R2B_RADDR(16'h{r2b['raddr']:x}), .R2B_PIPE(9'h{r2b['pipe']:x})
    ) dut (.clk(clk), .ce(1'b1), .rst(rst),
           .s_axis_tvalid(tvalid),
           .s_axis_tdata_re(stim[c][2*W-1:W]), .s_axis_tdata_im(stim[c][W-1:0]),
           .s_axis_tuser(tuser), .s_axis_tlast(tlast),
           .m_axis_tvalid(mvalid), .m_axis_tdata_re(ore), .m_axis_tdata_im(oim),
           .m_axis_tuser(muser), .m_axis_tlast(mlast));
    always #5 clk = ~clk;
    initial begin
        $readmemh("{stim}", stim);
        fd = $fopen("{HERE}/rtl_chain_{N}.txt", "w");
        repeat (2) @(negedge clk);
        rst = 0;
        for (c = 0; c < TOTAL + LAT + 4; c = c + 1) begin
            @(posedge clk);
            #1;
            if (c >= LAT - 1)
                $fwrite(fd, "%h %h\\n", ore, oim);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
""")
    vvp = os.path.join(HERE, f"chain_{N}.vvp")
    r = subprocess.run(["iverilog", "-g2005", "-s", "tb", "-o", vvp, tbv,
                        os.path.join(ROOT, "rtl", "fft_sdf_r23_dit.v"),
                        os.path.join(ROOT, "rtl", "fft_stage_r23_dit.v"),
                        os.path.join(ROOT, "rtl", "fft_sdf.v")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        return False
    r = subprocess.run(["vvp", vvp], capture_output=True, text=True,
                       cwd=HERE)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return False

    rtl = []
    with open(os.path.join(HERE, f"rtl_chain_{N}.txt")) as f:
        for line in f:
            a, b = line.split()
            if 'x' in a.lower() or 'x' in b.lower():
                break                    # frame-0 compare only; X = warmup
            va, vb = int(a, 16), int(b, 16)
            rtl.append((va - (1 << W) if va >> (W - 1) else va,
                        vb - (1 << W) if vb >> (W - 1) else vb))
    worst = 0
    first_bad = None
    for n in range(min(N, len(rtl))):
        d = max(abs(rtl[n][0] - exp[n][0]), abs(rtl[n][1] - exp[n][1]))
        if d > worst:
            worst = d
        if d > 0 and first_bad is None:
            first_bad = (n, rtl[n], exp[n])
    ok = worst == 0
    if verbose:
        print(f"N={N:6d} NS={NS} MMAX={MMAX} R={R} LAT={LAT} "
              f"worst|delta|={worst} {'PASS' if ok else 'FAIL'}")
        if first_bad:
            print(f"   first bad: n={first_bad[0]} rtl={first_bad[1]} "
                  f"golden={first_bad[2]}")
    return ok


def main():
    cases = [(512,), (1024,), (2048,)] if len(sys.argv) < 2 else \
            [(int(a),) for a in sys.argv[1:]]
    all_ok = True
    for (N,) in cases:
        all_ok &= run(N)
    print("ALL PASS" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
