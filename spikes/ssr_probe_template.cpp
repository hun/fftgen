// SSR debug probe (template; @VARS@ substituted by spikes/ssr_dbg.py).
// Feeds stimulus.txt as R-wide words and dumps per-cycle internals.
#include <cstdio>
#include <cstdint>
#include "Vfft_ssr.h"
#include "Vfft_ssr___024root.h"
#include "verilated.h"
int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Vfft_ssr* dut = new Vfft_ssr;
    auto& rr = *dut->rootp;
    FILE* fs = fopen("stimulus.txt", "r");
    long long sr[4096], si[4096]; int su[4096], sl[4096]; int Nn = 0;
    char h1[64], h2[64]; int u, l;
    while (fscanf(fs, "%63s %63s %d %d", h1, h2, &u, &l) == 4 && Nn < 4096) {
        sr[Nn] = strtoll(h1, nullptr, 16);
        si[Nn] = strtoll(h2, nullptr, 16);
        if (sr[Nn] & 0x8000) sr[Nn] -= 0x10000;
        if (si[Nn] & 0x8000) si[Nn] -= 0x10000;
        su[Nn] = u; sl[Nn] = l; Nn++;
    }
    fclose(fs);
    dut->rst = 1; dut->ce = 0; dut->s_axis_tvalid = 0;
    for (int i = 0; i < 6; i++) { dut->clk=!dut->clk; dut->eval();
                                  dut->clk=!dut->clk; dut->eval(); }
    dut->rst = 0;
    int fed = 0;
    for (long j = 0; j < @F@; j++) {
        bool feeding = fed < Nn;
        uint64_t tre = 0, tim = 0;
        if (feeding) for (int q = 0; q < @R@; q++) {
            tre |= ((uint64_t)(uint16_t)sr[fed*@R@+q]) << (q*@SW@);
            tim |= ((uint64_t)(uint16_t)si[fed*@R@+q]) << (q*@SW@);
        }
        dut->ce = 1; dut->s_axis_tvalid = feeding ? 1 : 0;
        dut->s_axis_tdata_re = tre; dut->s_axis_tdata_im = tim;
        dut->s_axis_tuser = feeding ? su[fed*@R@] : 0;
        dut->s_axis_tlast = feeding ? sl[fed*@R@+@R@-1] : 0;
        dut->clk = 1; dut->eval();
        printf("CYC j=%ld iv=%d "
               "l0=(%d,%d,%d,%d) l1=(%d,%d,%d,%d) "
               "p=%u pd=%u pd2=%u pd3=%u sc=%u sy=%u v1=%u v2=%u vl=%u "
               "o=(%d,%d|%d,%d) uv=%d\n",
            j, (int)dut->s_axis_tvalid,
            (int)(short)rr.fft_ssr__DOT__lanes__BRA__0__KET____DOT__ore,
            (int)(short)rr.fft_ssr__DOT__lanes__BRA__0__KET____DOT__oim,
            (int)rr.fft_ssr__DOT__lanes__BRA__0__KET____DOT__ou,
            (int)rr.fft_ssr__DOT__lanes__BRA__0__KET____DOT__v,
            (int)(short)rr.fft_ssr__DOT__lanes__BRA__1__KET____DOT__ore,
            (int)(short)rr.fft_ssr__DOT__lanes__BRA__1__KET____DOT__oim,
            (int)rr.fft_ssr__DOT__lanes__BRA__1__KET____DOT__ou,
            (int)rr.fft_ssr__DOT__lanes__BRA__1__KET____DOT__v,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__p,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__pd,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__pd2,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__pd3,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__scnt,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__synced,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__v1,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__v2,
            (unsigned)rr.fft_ssr__DOT__u_cross__DOT__vlast,
            (int)(short)(dut->m_axis_tdata_re & 0xffff),
            (int)(short)((dut->m_axis_tdata_re>>16) & 0xffff),
            (int)(short)(dut->m_axis_tdata_im & 0xffff),
            (int)(short)((dut->m_axis_tdata_im>>16) & 0xffff),
            (int)dut->m_axis_tvalid);
        dut->clk = 0; dut->eval();
        if (feeding) fed++;
    }
    return 0;
}
