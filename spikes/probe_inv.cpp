// Probe fft_ssr_r22_inv wrapper internals per cycle against the golden
// model's a0/a1/q1. Build from the generated build dir (RTL copied there):
//   verilator --cc --exe --build -j4 --top-module fft_ssr_r22_inv \
//     --public-flat-rw -Wno-fatal \
//     -CFLAGS "-DTB_SAMPLE_WIDTH=16 -DTB_OUTPUT_WIDTH=16 -DTB_SSR=2" \
//     -GNUM_POINTS=8 -GSSR=2 -GSAMPLE_WIDTH=16 -GSAMPLE_DECIMAL=0 \
//     -GOUTPUT_WIDTH=16 -GOUTPUT_DECIMAL=0 -GTWIDDLE_WIDTH=18 \
//     -GTWIDDLE_DECIMAL=17 -GINTERN_WIDTH=17 -GPIPE_DEPTH=10 -GINVERSE=1 \
//     fft_ssr_r22_inv.v fft_top_r22.v fft_sdf_r22.v fft_stage_r22.v \
//     fft_sdf.v fft_reorder.v probe_inv.cpp
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <string>
#include <vector>
#include "Vfft_ssr_r22_inv.h"
#include "Vfft_ssr_r22_inv___024root.h"
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
static int64_t se(uint64_t v, int n) {
    int64_t m = (int64_t)1 << (n - 1);
    v &= ((uint64_t)1 << n) - 1;
    return (v & m) ? (int64_t)(v | ~(((uint64_t)1 << n) - 1)) : (int64_t)v;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    std::ifstream fstim("stimulus.txt");
    std::vector<Smp> stim;
    std::string line;
    while (std::getline(fstim, line)) {
        if (line.empty() || line[0] == '#') continue;
        long long a, b; int u, l;
        if (sscanf(line.c_str(), "%llx %llx %d %d", &a, &b, &u, &l) != 4) continue;
        stim.push_back({a, b, u, l});
    }
    Vfft_ssr_r22_inv* d = new Vfft_ssr_r22_inv;
    d->rst = 1; d->ce = 0; d->s_axis_tvalid = 0;
    d->s_axis_tdata_re = 0; d->s_axis_tdata_im = 0;
    d->s_axis_tuser = 0; d->s_axis_tlast = 0;
    d->clk = 0;
    for (int i = 0; i < 4; i++) { d->clk=!d->clk; d->eval(); d->clk=!d->clk; d->eval(); }
    d->rst = 0;
    size_t fed = 0;
    int maxc = 14;
    for (int c = 0; c < maxc; c++) {
        bool feeding = (fed < stim.size() / 2);
        int64_t wre[2] = {0, 0}, wim[2] = {0, 0};
        int wu = 0, wl = 0;
        if (feeding) {
            for (int r = 0; r < 2; r++) {
                Smp s = stim[fed * 2 + r];
                wre[r] = s.re; wim[r] = s.im;
            }
            wu = stim[fed * 2].u;
            wl = stim[fed * 2 + 1].l;
            fed++;
        }
        d->ce = 1; d->s_axis_tvalid = 1;
        d->s_axis_tdata_re = (uint64_t)(uint16_t)wre[0] | ((uint64_t)(uint16_t)wre[1] << 16);
        d->s_axis_tdata_im = (uint64_t)(uint16_t)wim[0] | ((uint64_t)(uint16_t)wim[1] << 16);
        d->s_axis_tuser = wu; d->s_axis_tlast = wl;
        d->clk = 1; d->eval();
        auto& r = *d->rootp;
        printf("c=%d word=%zu a0=(%lld,%lld) a1=(%lld,%lld) wq=(%lld,%lld) "
               "r0=(%lld,%lld) r1=(%lld,%lld) m_ri=%lld m_ir=%lld t_im=%lld "
               "tv=%d mkv=%d\n",
               c, (size_t)(c >= 3 ? c - 3 : 0),
               (long long)se(r.fft_ssr_r22_inv__DOT__a0_re, 17),
               (long long)se(r.fft_ssr_r22_inv__DOT__a0_im, 17),
               (long long)se(r.fft_ssr_r22_inv__DOT__a1_re, 17),
               (long long)se(r.fft_ssr_r22_inv__DOT__a1_im, 17),
               (long long)se(r.fft_ssr_r22_inv__DOT__wq_re, 18),
               (long long)se(r.fft_ssr_r22_inv__DOT__wq_im, 18),
               (long long)se(r.fft_ssr_r22_inv__DOT__r0_re, 16),
               (long long)se(r.fft_ssr_r22_inv__DOT__r0_im, 16),
               (long long)se(r.fft_ssr_r22_inv__DOT__r1_re, 16),
               (long long)se(r.fft_ssr_r22_inv__DOT__r1_im, 16),
               (long long)se(r.fft_ssr_r22_inv__DOT__m_ri, 35),
               (long long)se(r.fft_ssr_r22_inv__DOT__m_ir, 35),
               (long long)se(r.fft_ssr_r22_inv__DOT__t_im, 36),
               (int)((r.fft_ssr_r22_inv__DOT__tv >> 3) & 1),
               (int)((r.fft_ssr_r22_inv__DOT__mk_u >> 3) & 1));
        d->clk = 0; d->eval();
    }
    return 0;
}
