#!/usr/bin/env python3
"""RTL bring-up for fft_stage_r23_dit (S8): bit-exact vs golden._R23DITStage.

Stimulus = the DIF->DIT round-trip stream (golden._R23DIFStage output,
the DIT stage's real input format). The tb feeds dout[q] at clock
c = q-L1 (so the DUT's phase k = the golden's pos), samples the output
register after each edge, and the script compares RTL vs golden:

    rtl_out[c]  ==  rets[c+1]        (the value the golden computed at
                                      pos=c; the golden's return at the
                                      call pos=P is computed at P-1)
"""
import math
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # project root
sys.path.insert(0, os.path.join(ROOT, "src"))
from golden import _R23DIFStage, _R23DITStage
from twiddles import canonical_twiddles

TD, TW, W = 17, 18, 16


def q8_of(td):
    return int(math.floor(math.sqrt(2) / 2 * (1 << td) + 0.5))


def bitrev3(v):
    return ((v & 1) << 2) | (v & 2) | ((v >> 2) & 1)


def run(N, m, blocks=3, seed=1234, kp=0, verbose=True):
    G = N >> (3 * m + 3)
    base = 8 ** m
    tw_inv = canonical_twiddles(N, TW, TD, True)
    tw_fwd = canonical_twiddles(N, TW, TD, False)
    L1, L2 = 7 * G + 2, 7 * G + 1
    T = blocks * 8 * G

    rng = random.Random(seed + N + m)
    xin = [(rng.randint(-8, 8), rng.randint(-8, 8)) for _ in range(T)]
    dif = _R23DIFStage(m, N, 0, 0, 0, TD, tw_inv, inverse=True)
    dout = [dif.step(xin[p], p) for p in range(T)]
    for _ in range(L1 + 8):
        dout.append(dif.step((0, 0), T + len(dout)))

    dit = _R23DITStage(m, N, 0, 0, 0, TD, tw_fwd, inverse=False,
                       shift_extra=3)
    rets = []
    for q in range(L1, L1 + T):
        rets.append(dit.step(dout[q], q - L1))
    rets.append(dit.step((0, 0), T))          # rets[P] for P = 0..T

    # ---- twiddle mem (window-ordered slices): word (s*G+g) =
    #      T_fwd[BR[s+1]*g*base mod N], s = 0..6 ----
    twf = os.path.join(HERE, f"tw_dit_{N}_{m}.mem")
    with open(twf, "w") as f:
        for s in range(7):
            k = bitrev3(s + 1)
            for g in range(G):
                re, im = tw_fwd[(k * g * base) % N]
                f.write(f"{(re & (2**TW-1)) << TW | (im & (2**TW-1)):x}\n")

    # ---- stimulus: dout[q] at clock c = q-L1 ----
    stim = os.path.join(HERE, f"stim_{N}_{m}.txt")
    with open(stim, "w") as f:
        for q in range(L1, L1 + T):
            re, im = dout[q]
            f.write(f"{(re & (2**W-1)) << W | (im & (2**W-1)):x}\n")

    # ---- tb ----
    tbv = os.path.join(HERE, "tb_dit.v")
    with open(tbv, "w") as f:
        f.write(f"""`timescale 1ns/1ps
module tb;
    localparam integer W = {W};
    localparam integer T = {T};
    reg clk = 0, ce = 1, rst = 1;
    reg signed [W-1:0] in_re = 0, in_im = 0;
    wire signed [W-1:0] out_re, out_im;
    reg [2*W-1:0] stim [0:T-1];
    integer c;
    integer fd;
    fft_stage_r23_dit #(
        .DEPTH({G}), .WIDTH({W}),
        .SIGMA0(1), .SIGMA1(1), .SIGMA2(1),
        .TWIDDLE_WIDTH({TW}), .TWIDDLE_DECIMAL({TD}),
        .ROM_BASE(0), .NPTS({N}), .INVERSE(0), .Q8({q8_of(TD)}),
        .K_PRELOAD({kp}), .TWIDDLE_FILE("{twf}")
    ) dut (.clk(clk), .ce(ce), .rst(rst),
           .in_re(in_re), .in_im(in_im),
           .out_re(out_re), .out_im(out_im));
    always #5 clk = ~clk;
    initial begin
        $readmemh("{stim}", stim);
        fd = $fopen("{HERE}/rtl_out_{N}_{m}.txt", "w");
        repeat (2) @(negedge clk);
        rst = 0;
        for (c = 0; c < T; c = c + 1) begin
            in_re = $signed(stim[c][2*W-1:W]);
            in_im = $signed(stim[c][W-1:0]);
            @(posedge clk);
            #1;
            $fwrite(fd, "%h %h\\n", out_re, out_im);
            @(negedge clk);
        end
        $fclose(fd);
        $finish;
    end
endmodule
""")

    vvp = os.path.join(HERE, f"dit_{N}_{m}.vvp")
    r = subprocess.run(["iverilog", "-g2005", "-o", vvp, tbv,
                        os.path.join(ROOT, "rtl", "fft_stage_r23_dit.v")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr)
        return False, -1
    r = subprocess.run(["vvp", vvp], capture_output=True, text=True,
                       cwd=HERE)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return False, -1

    # ---- compare ----
    rtl = []
    with open(os.path.join(HERE, f"rtl_out_{N}_{m}.txt")) as f:
        for line in f:
            a, b = line.split()
            rtl.append((int(a, 16) - (1 << W) if int(a, 16) >> (W - 1)
                        else int(a, 16),
                        int(b, 16) - (1 << W) if int(b, 16) >> (W - 1)
                        else int(b, 16)))
    worst = 0
    checked = 0
    first_bad = None
    for c in range(T - 1):
        if c < L2:
            continue
        gr, gi = rets[c + 1]
        assert -2**(W-1) <= gr < 2**(W-1) and -2**(W-1) <= gi < 2**(W-1), \
            f"golden value exceeds {W} bits at pos {c+1}: {gr},{gi}"
        d = max(abs(rtl[c][0] - gr), abs(rtl[c][1] - gi))
        checked += 1
        if d > worst:
            worst = d
        if d > 0 and first_bad is None:
            first_bad = (c, rtl[c], (gr, gi))
    ok = worst == 0 and checked > 0
    if verbose:
        print(f"N={N:6d} m={m} G={G:5d} checked={checked:6d} "
              f"max|delta|={worst} {'PASS' if ok else 'FAIL'}")
        if first_bad:
            print(f"   first bad: clock={first_bad[0]} "
                  f"rtl={first_bad[1]} golden={first_bad[2]}")
    return ok, worst


def main():
    cases = [(1024, 0), (4096, 1)] if len(sys.argv) < 2 else \
            [(int(a.split(",")[0]), int(a.split(",")[1]))
             for a in sys.argv[1:]]
    all_ok = True
    for N, m in cases:
        ok, _ = run(N, m)
        all_ok &= ok
    print("ALL PASS" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
