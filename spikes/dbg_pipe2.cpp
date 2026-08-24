#include <cstdio>
#include "Vfft_sdf.h"
#include "Vfft_sdf___024root.h"

#include "verilated.h"

static int64_t se(int64_t v, int n) {
    int64_t m = (int64_t)1 << (n - 1);
    return (v & m) ? (v | ~(((int64_t)1 << n) - 1)) : v;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Vfft_sdf* d = new Vfft_sdf;
    d->rst = 1; d->ce = 0; d->s_axis_tvalid = 0;
    for (int i = 0; i < 6; i++) { d->clk=!d->clk; d->eval(); d->clk=!d->clk; d->eval(); }
    d->rst = 0;
    int64_t data[2][2] = {{9677,-12996},{18982,-26440}};
    for (int c = 0; c < 14; c++) {
        int idx = c % 2;
        d->ce = 1; d->s_axis_tvalid = 1;
        d->s_axis_tdata_re = data[idx][0]; d->s_axis_tdata_im = data[idx][1];
        d->clk = 1; d->eval();
        auto& r = *d->rootp;
        printf("c=%d comp=%d flags=%02x d=%lld a=%lld bd=%lld mr=%lld cs=%lld cp=%lld out=(%d,%d)\n",
               c+1,
               (int)r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage__DOT__in_compute,
               (int)r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage__DOT__pipe_comp,
               (long long)se(r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage__DOT__d_reg_re,17),
               (long long)se(r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage__DOT__a_reg_re,17),
               (long long)se(r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage__DOT__bfly_d_re,17),
               (long long)se(r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage__DOT__mreg_re,40),
               (long long)se(r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage__DOT__comb_s_re,40),
               (long long)se(r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage__DOT__comb_p_re,40),
               (int)d->m_axis_tdata_re, (int)d->m_axis_tdata_im);
        d->clk = 0; d->eval();
    }
    return 0;
}
