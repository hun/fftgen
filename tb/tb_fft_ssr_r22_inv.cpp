// fft_ssr_r22_inv Verilator testbench: R-wide word interface over the flat
// stimulus stream. Reads stimulus.txt (re im user last per line, hex,
// bitrev_N order), groups into R-sample words, drives fft_ssr_r22_inv,
// captures valid output words flattened into actual.txt (one line per
// sample). Identical to tb_fft_ssr_r22.cpp except the DUT class name.
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <string>
#include <vector>
#include "Vfft_ssr_r22_inv.h"
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

#ifndef TB_SSR
#define TB_SSR 2
#endif


// Wide packed-bus helpers: for R >= 8 the R*SW-bit axis words exceed
// 64 bits and Verilator models them as VlWide<4> (128-bit).
#if defined(TB_SSR) && (TB_SSR >= 8)
typedef VlWide<4> BusT;
static void bus_set(BusT& b, uint64_t lo, uint64_t hi) {
    b[0] = (uint32_t)lo;         b[1] = (uint32_t)(lo >> 32);
    b[2] = (uint32_t)hi;         b[3] = (uint32_t)(hi >> 32);
}
static uint64_t bus_get(const BusT& b, int bit) {
    // extract up to 64 bits starting at bit position 'bit'
    uint64_t lo = ((uint64_t)b[1] << 32) | b[0];
    uint64_t hi = ((uint64_t)b[3] << 32) | b[2];
    if (bit == 0) return lo;
    if (bit < 64) return (lo >> bit) | (hi << (64 - bit));
    return hi >> (bit - 64);
}
#else
typedef QData BusT;
static void bus_set(BusT& b, uint64_t lo, uint64_t) { b = (QData)lo; }
static uint64_t bus_get(const BusT& b, int bit) { return b >> bit; }
#endif

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    const int R = TB_SSR;
#ifdef TB_SAMPLE_WIDTH
    const int SW = TB_SAMPLE_WIDTH;
#else
    const int SW = 16;
#endif
#ifdef TB_OUTPUT_WIDTH
    const int OW = TB_OUTPUT_WIDTH;
#else
    const int OW = 16;
#endif

    std::ifstream fstim("stimulus.txt");
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
    size_t total_words = stim.size() / R;
    if (total_words == 0) { fprintf(stderr, "empty stimulus\n"); return 2; }

    size_t exp_count = 0;
    while (std::getline(fgen, line)) {
        if (!line.empty() && line[0] != '#') exp_count++;
    }

    Vfft_ssr_r22_inv* dut = new Vfft_ssr_r22_inv;
    dut->rst = 1; dut->ce = 0; dut->s_axis_tvalid = 0;
    { BusT zr, zi; bus_set(zr,0,0); bus_set(zi,0,0);
      dut->s_axis_tdata_re = zr; dut->s_axis_tdata_im = zi; }
    dut->s_axis_tuser = 0; dut->s_axis_tlast = 0;
    dut->clk = 0;
    for (int i = 0; i < 4; i++) {
        dut->clk = !dut->clk; dut->eval();
        dut->clk = !dut->clk; dut->eval();
    }
    dut->rst = 0;

    std::ofstream fout("actual.txt");
    size_t fed_words = 0, got_samples = 0;
    uint64_t cycle = 0;
    int j_dbg = 0;
    while (got_samples < exp_count) {
        bool feeding = (fed_words < total_words);
        int64_t wre[8] = {0}, wim[8] = {0};
        int wu = 0, wl = 0;
        if (feeding) {
            for (int r = 0; r < R; r++) {
                Smp s = stim[fed_words * R + r];
                wre[r] = s.re; wim[r] = s.im;
            }
            // SOF rides the frame's first word; EOF the last word
            wu = stim[fed_words * R].u;
            wl = stim[fed_words * R + R - 1].l;
            fed_words++;
        }
        dut->ce = 1;
        // drain: keep enabled with zero data so lanes flush (matches
        // the R=1 testbench and the golden model's drain semantics)
        dut->s_axis_tvalid = 1;
        uint64_t tre_lo = 0, tre_hi = 0, tim_lo = 0, tim_hi = 0;
        for (int r = 0; r < R; r++) {
            if (r * SW < 64) {
                tre_lo |= ((uint64_t)(uint16_t)wre[r]) << (r * SW);
                tim_lo |= ((uint64_t)(uint16_t)wim[r]) << (r * SW);
            } else {
                tre_hi |= ((uint64_t)(uint16_t)wre[r]) << (r * SW - 64);
                tim_hi |= ((uint64_t)(uint16_t)wim[r]) << (r * SW - 64);
            }
        }
        { BusT br, bi; bus_set(br, tre_lo, tre_hi);
          bus_set(bi, tim_lo, tim_hi);
          dut->s_axis_tdata_re = br; dut->s_axis_tdata_im = bi; }
        dut->s_axis_tuser = wu;
        dut->s_axis_tlast = wl;
        dut->clk = 1; dut->eval();
        if (getenv("TB_DEBUG") && j_dbg < 60) {
            fprintf(stderr, "cyc=%zu fed=%zu tv=%d lv=%d\n", cycle,
                    fed_words, (int)dut->s_axis_tvalid,
                    (int)dut->m_axis_tvalid);
            j_dbg++;
        }
        if (dut->m_axis_tvalid) {
            for (int q = 0; q < R; q++) {
                uint64_t ore = bus_get(dut->m_axis_tdata_re, q * OW);
                uint64_t oim = bus_get(dut->m_axis_tdata_im, q * OW);
                uint64_t msk = ((uint64_t)1 << OW) - 1;
                int64_t mre = (ore & msk) & (((int64_t)1) << (OW-1))
                              ? ((int64_t)(ore & msk)) - (((int64_t)1)<<OW)
                              : (int64_t)(ore & msk);
                int64_t mim = (oim & msk) & (((int64_t)1) << (OW-1))
                              ? ((int64_t)(oim & msk)) - (((int64_t)1)<<OW)
                              : (int64_t)(oim & msk);
                int uu = (q == 0) ? (int)dut->m_axis_tuser : 0;
                int ll = (q == R-1) ? (int)dut->m_axis_tlast : 0;
                fout << mre << " " << mim << " " << uu << " " << ll << "\n";
                got_samples++;
            }
        }
        dut->clk = 0; dut->eval();
        cycle++;
        if (cycle > 10'000'000) { fprintf(stderr, "timeout\n"); return 3; }
    }
    fout.close();
    delete dut;
    fprintf(stderr, "ok: %zu samples\n", got_samples);
    return 0;
}
