// fft_sdf Verilator testbench: reads stimulus (re im user last per line,
// hex), drives the core with optional freeze mask, captures all valid
// outputs into actual.txt. Comparison with expected.txt is done by the
// Python harness (single source of truth = golden model).

#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <string>
#include <vector>
#include "Vfft_top.h"
#include "verilated.h"

struct Smp { int64_t re, im; int u, l; };

static int64_t parse_hex(const std::string& s, int width) {
    uint64_t v = strtoull(s.c_str(), nullptr, 16);
    int64_t sign = (int64_t)1 << (width - 1);
    int64_t mask = ((int64_t)1 << width) - 1;
    int64_t r = v & mask;
    if (r & sign) r -= ((int64_t)1 << width);
    return r;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    // parameters (compile-time defines from fft_gen.py)
#ifndef TB_SAMPLE_WIDTH
#define TB_SAMPLE_WIDTH 16
#endif
#ifndef TB_OUTPUT_WIDTH
#define TB_OUTPUT_WIDTH 16
#endif
    int SW = TB_SAMPLE_WIDTH;
    int OW = TB_OUTPUT_WIDTH;

    std::ifstream fstim("stimulus.txt");
    std::ifstream fmask("mask.txt");
    std::ifstream fgen("expected.txt");
    if (!fstim || !fgen) { fprintf(stderr, "stimulus/expected missing\n"); return 2; }

    std::vector<Smp> stim;
    std::string line;
    while (std::getline(fstim, line)) {
        if (line.empty() || line[0] == '#') continue;
        long long a, b; int u, l;
        if (sscanf(line.c_str(), "%llx %llx %d %d", &a, &b, &u, &l) != 4) continue;
        stim.push_back({a, b, u, l});
    }
    size_t total = stim.size();
    if (total == 0) { fprintf(stderr, "empty stimulus\n"); return 2; }

    // expected count = number of expected lines
    size_t exp_count = 0;
    while (std::getline(fgen, line)) {
        if (!line.empty() && line[0] != '#') exp_count++;
    }

    // optional mask (cycled)
    std::vector<int> mask;
    if (fmask) {
        std::string mline;
        while (std::getline(fmask, mline)) {
            if (!mline.empty() && (mline[0] == '0' || mline[0] == '1'))
                mask.push_back(mline[0] == '1');
        }
    }

    Verilated::traceEverOn(false);
    Vfft_top* dut = new Vfft_top;
    dut->rst = 1;
    dut->ce = 0;
    dut->s_axis_tvalid = 0;
    dut->s_axis_tdata_re = 0;
    dut->s_axis_tdata_im = 0;
    dut->s_axis_tuser = 0;
    dut->s_axis_tlast = 0;
    // clock in reset
    dut->clk = 0;
    for (int i = 0; i < 4; i++) {
        dut->clk = !dut->clk; dut->eval();
        dut->clk = !dut->clk; dut->eval();
    }
    dut->rst = 0;

    std::ofstream fout("actual.txt");
    size_t fed = 0, got = 0;
    uint64_t cycle = 0;
    int64_t din_re = 0, din_im = 0;
    int din_u = 0, din_l = 0;
    while (got < total) {
        bool feeding = (fed < total);
        bool en = true;
        if (!mask.empty()) en = mask[cycle % mask.size()];
        if (!feeding) en = true;               // drain: always enabled, zero data
        if (feeding && en) {
            din_re = stim[fed].re;
            din_im = stim[fed].im;
            din_u  = stim[fed].u;
            din_l  = stim[fed].l;
            fed++;
        }
        dut->ce = 1;
        dut->s_axis_tvalid = en ? 1 : 0;
        if (!feeding) { din_re = 0; din_im = 0; din_u = 0; din_l = 0; }
        dut->s_axis_tdata_re = din_re;
        dut->s_axis_tdata_im = din_im;
        dut->s_axis_tuser = din_u;
        dut->s_axis_tlast = din_l;
        dut->clk = 1; dut->eval();
        if (dut->m_axis_tvalid) {
            int64_t ore = (int64_t)dut->m_axis_tdata_re;
            int64_t oim = (int64_t)dut->m_axis_tdata_im;
            // sign-extend to 64 bits for printing
            int64_t mre = (ore & ((int64_t)1 << (OW-1))) ? (ore | ~(((int64_t)1 << OW)-1)) : ore;
            int64_t mim = (oim & ((int64_t)1 << (OW-1))) ? (oim | ~(((int64_t)1 << OW)-1)) : oim;
            // print as signed decimal (harness compares numerically)
            fout << mre << " " << mim << " "
                 << (int)dut->m_axis_tuser << " " << (int)dut->m_axis_tlast << "\n";
            got++;
        }
        dut->clk = 0; dut->eval();
        cycle++;
        if (cycle > 10'000'000) { fprintf(stderr, "timeout\n"); return 3; }
    }
    fout.close();
    delete dut;
    if (got != exp_count) {
        fprintf(stderr, "count mismatch: got %zu expected %zu\n", got, exp_count);
        return 1;
    }
    fprintf(stderr, "ok: %zu samples\n", got);
    return 0;
}
