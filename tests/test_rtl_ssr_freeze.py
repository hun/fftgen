"""PLAN.md 2.8 freeze contract for the SSR crossbar (fft_cross.v).

Regression for the missing ``&& run`` term on ``out_valid``: a frozen
cycle (ce or in_valid low) must deassert m_axis_tvalid so a consumer
never sees a word twice. The R=1 path (fft_sdf.v) implements this
contract and is freeze-tested; the SSR path had no coverage, and
fft_cross.v held out_valid high during freezes.

The test drives fft_cross alone (no lanes) with 4 frames -- zeros,
zeros, impulse, zeros -- freezes ce/in_valid for 4 word-cycles before
the impulse frame, and asserts:
  * out_valid drops on the frozen cycles (the bug: it stayed high);
  * emission resumes after the freeze with the delayed pre-freeze words
    and the impulse frame intact (flat spectrum ~A/N per bin);
  * SOF/EOF markers still ride their frame's first/last emitted word.
"""
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from config import FFTConfig
from fft_gen import write_wn_mem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RTL = os.path.join(ROOT, "rtl")

N, R = 16, 2
M = N // R
FREEZE_AT, FREEZE_LEN = 24, 4   # word-cycles 24..27 (after sync)

# CB_LAT = 7 at R=2: a word entered at din-cycle w exits at dout-cycle
# w + 6. Frame sync (mature = scnt > CB_LAT+1) drops the fill frames
# and starts emission at the first p==0 word whose output is mature:
# word 8 at dout 14, then the impulse frame (words 16..23) at dout
# 22..29. The freeze at din 24..27 (frame 3's first words, after sync)
# must drop out_valid (the regression); in-flight words 18..23 exit
# delayed at dout 28..33; words 24..27 never enter and are absent.
#
# The crossbar consumes LANE OUTPUTS (spectra at Q(output_decimal)):
# the M=8 lane FFT of an impulse at the frame start is flat 4096/8 =
# 512 on lane 0 and 0 on lane 1 for every word of the impulse frame
# (words 16..23); the crossbar's /R=2 then gives 256 per bin on both
# output lanes (verified against SSRGoldenModel.tick).

TB = r"""// fft_cross freeze-contract unit testbench: drives 4 frames (frames
// 0,1,3 zero; frame 2 an impulse at sample 0), freezes ce/in_valid for
// FREEZE_LEN word-cycles at FREEZE_AT, records per-cycle out_valid into
// valid_trace.txt and emitted words into actual.txt.
#include <cstdio>
#include <cstdint>
#include <fstream>
#include <vector>
#include "Vfft_cross.h"
#include "verilated.h"

#ifndef TB_M
#define TB_M 8
#endif
#ifndef TB_R
#define TB_R 2
#endif
#ifndef TB_SW
#define TB_SW 16
#endif
#ifndef TB_OW
#define TB_OW 16
#endif
#ifndef TB_FRAMES
#define TB_FRAMES 4
#endif
#ifndef FREEZE_AT
#define FREEZE_AT 12
#endif
#ifndef FREEZE_LEN
#define FREEZE_LEN 4
#endif

static int64_t sign_ext(uint64_t v, int w) {
    uint64_t m = ((uint64_t)1 << w) - 1;
    v &= m;
    if (v & ((uint64_t)1 << (w - 1))) v -= ((uint64_t)1 << w);
    return (int64_t)v;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    // lane outputs: frame 2 (words 16..23, 8 words/frame at R=2, M=8)
    // carries the flat impulse spectrum; everything else is zero
    int nwords = TB_FRAMES * TB_M / TB_R;

    Vfft_cross* dut = new Vfft_cross;
    dut->rst = 1; dut->ce = 0; dut->in_valid = 0;
    dut->in_user = 0; dut->in_last = 0;
    dut->din_re = 0; dut->din_im = 0;
    dut->clk = 0;
    for (int i = 0; i < 4; i++) {
        dut->clk = !dut->clk; dut->eval();
        dut->clk = !dut->clk; dut->eval();
    }
    dut->rst = 0;

    std::ofstream trace("valid_trace.txt");
    std::ofstream fout("actual.txt");
    int total = nwords + 48;            // + drain (zero words, valid)
    for (int c = 0; c < total; c++) {
        bool frozen = (c >= FREEZE_AT && c < FREEZE_AT + FREEZE_LEN);
        dut->ce = frozen ? 0 : 1;
        dut->in_valid = frozen ? 0 : 1;
        uint64_t tre = 0, tim = 0;
        if (c < nwords && !frozen) {
            for (int r = 0; r < TB_R; r++) {
                // word c: lane 0 = the flat lane spectrum (512) within
                // the impulse frame, else 0
                int64_t v = ((c >= 16 && c < 24) && r == 0)
                            ? 512 : 0;
                tre |= ((uint64_t)(uint16_t)v) << (r * TB_SW);
                tim |= 0;
            }
            // SOF on each frame's first word, EOF on each frame's last
            dut->in_user = (c % TB_M == 0) ? 1 : 0;
            dut->in_last = (c % TB_M == TB_M - 1) ? 1 : 0;
        } else {
            dut->in_user = 0; dut->in_last = 0;
        }
        dut->din_re = tre; dut->din_im = tim;
        dut->clk = 1; dut->eval();
        trace << (dut->out_valid ? 1 : 0) << "\n";
        if (dut->out_valid) {
            for (int q = 0; q < TB_R; q++) {
                int64_t ore = sign_ext((uint64_t)dut->dout_re >> (q * TB_OW),
                                       TB_OW);
                int64_t oim = sign_ext((uint64_t)dut->dout_im >> (q * TB_OW),
                                       TB_OW);
                int uu = (q == 0) ? (int)dut->out_user : 0;
                int ll = (q == TB_R - 1) ? (int)dut->out_last : 0;
                fout << ore << " " << oim << " " << uu << " " << ll << "\n";
            }
        }
        dut->clk = 0; dut->eval();
    }
    trace.close(); fout.close();
    delete dut;
    return 0;
}
"""


def _build(outdir):
    os.makedirs(outdir, exist_ok=True)
    cfg = FFTConfig(num_points=N, ssr=R, output_order="native")
    write_wn_mem(cfg, os.path.join(outdir, "fft_wn.mem"))
    shutil.copy(os.path.join(RTL, "fft_cross.v"), outdir)
    with open(os.path.join(outdir, "tb_freeze.cpp"), "w") as f:
        f.write(TB)
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", "fft_cross", "-Wno-fatal",
           "-CFLAGS", f"-DTB_M={M} -DTB_R={R} -DTB_SW=16 -DTB_OW=16 "
                      f"-DTB_FRAMES=8 -DFREEZE_AT={FREEZE_AT} "
                      f"-DFREEZE_LEN={FREEZE_LEN}",
           "-GNUM_POINTS=16", "-GSSR=2", "-GOUT_WIDTH=16", "-GOUT_DECIMAL=0",
           "-GTWIDDLE_WIDTH=18", "-GTWIDDLE_DECIMAL=17",
           "-GINVERSE=0", "-GEMIT_BREV=0",
           "fft_cross.v", "tb_freeze.cpp"]
    r = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"verilator build failed:\n{r.stderr[-2000:]}")
    r = subprocess.run([os.path.join(outdir, "obj_dir", "Vfft_cross")],
                       cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"sim failed:\n{r.stderr[-2000:]}")


class TestCrossbarFreeze(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outdir = os.path.join(ROOT, "build", "test_ssr_freeze")
        shutil.rmtree(cls.outdir, ignore_errors=True)
        _build(cls.outdir)
        with open(os.path.join(cls.outdir, "valid_trace.txt")) as f:
            cls.trace = [int(x) for x in f.read().split()]
        cls.entries = []
        with open(os.path.join(cls.outdir, "actual.txt")) as f:
            for ln in f:
                p = ln.split()
                if len(p) == 4:
                    cls.entries.append((int(p[0]), int(p[1]), int(p[2]),
                                        int(p[3])))

    def test_out_valid_drops_on_frozen_cycles(self):
        # the bug: out_valid stayed high on frozen cycles (no && run);
        # with the fix it must drop even though synced is latched
        for c in range(FREEZE_AT, FREEZE_AT + FREEZE_LEN):
            self.assertEqual(self.trace[c], 0,
                             f"out_valid high on frozen cycle {c}")

    def test_emission_resumes_after_freeze(self):
        # the first emitted word (the p0) exits at dout 14, and the
        # impulse frame's in-flight words exit delayed at 28..33
        self.assertEqual(self.trace[14], 1, "sync word not emitted")
        for c in range(FREEZE_AT + FREEZE_LEN, FREEZE_AT + FREEZE_LEN + 12):
            self.assertEqual(self.trace[c], 1,
                             f"out_valid low on running cycle {c}")

    def test_no_output_before_frame_sync(self):
        # sync is at dout-cycle 14 (the p0); the fill frames are not
        # emitted
        for c in range(0, 14):
            self.assertEqual(self.trace[c], 0,
                             f"out_valid high pre-sync at cycle {c}")

    def test_impulse_frame_intact_with_markers(self):
        # frame 2 (impulse) must emerge flat ~A/N = 256 per bin, with
        # its SOF on the first emitted word and EOF on the last
        imp = [i for i, e in enumerate(self.entries)
               if abs(e[0] - 256) <= 2 and abs(e[1]) <= 2]
        self.assertEqual(len(imp), 16,
                         "impulse frame must be exactly 16 samples")
        self.assertEqual(self.entries[imp[0]][2], 1, "SOF not on frame 2")
        self.assertEqual(self.entries[imp[-1]][3], 1, "EOF not on frame 2")
        # everything before the impulse frame is zero (fill frames)
        for e in self.entries[:imp[0]]:
            self.assertEqual(e[:2], (0, 0))


if __name__ == "__main__":
    unittest.main()
