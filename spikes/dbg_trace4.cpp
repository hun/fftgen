// cycle-accurate stage-internals trace for N=4 vs golden model
#include <cstdio>
#include <vector>
#include "Vfft_sdf.h"
#include "Vfft_sdf___024root.h"
#include "verilated.h"

static int64_t se(int64_t v, int n) {
    int64_t m = (int64_t)1 << (n - 1);
    return (v & m) ? (v | ~(((int64_t)1 << n) - 1)) : v;
}

#define DUMPF(c, g, p, fmt, ...) printf( \
    "c=%02d s%d " fmt "\n", \
    c, g, __VA_ARGS__)

#define DUMP(c, g, p) DUMPF(c, g, p, \
    "comp=%d i=%d w=%d pw=%d ra=%d pipe=%x d=%lld out=(%lld,%lld)", \
    (int)p##__DOT__##in_compute, (int)p##__DOT__##phase_i, \
    (int)p##__DOT__##wptr, (int)p##__DOT__##pwp, \
    (int)p##__DOT__##raddr_r, (int)p##__DOT__##pipe_comp, \
    (long long)se(p##__DOT__##d_reg_re, 17), \
    (long long)se(r.fft_sdf__DOT__st_out_re[g] & 0x1FFFF, 17), \
    (long long)se(r.fft_sdf__DOT__st_out_im[g] & 0x1FFFF, 17))

#define DUMP3(c, g, p) DUMPF(c, g, p, \
    "MEM pr=%d pfifo=(%lld,%lld|%lld,%lld) ram=(%lld,%lld|%lld,%lld)", \
    (int)p##__DOT__##pr, \
    (long long)se(p##__DOT__##pfifo_re[0], 17), \
    (long long)se(p##__DOT__##pfifo_im[0], 17), \
    (long long)se(p##__DOT__##pfifo_re[1], 17), \
    (long long)se(p##__DOT__##pfifo_im[1], 17), \
    (long long)se(p##__DOT__##ram_re[0], 17), \
    (long long)se(p##__DOT__##ram_im[0], 17), \
    (long long)se(p##__DOT__##ram_re[1], 17), \
    (long long)se(p##__DOT__##ram_im[1], 17))

#define DUMP2(c, g, p) DUMPF(c, g, p, \
    "DATA dd2=(%lld,%lld) bfld=%lld bfls=%lld adly=%lld twd1=%lld " \
    "mreg=(%lld,%lld) combS=(%lld,%lld) combP=(%lld,%lld)", \
    (long long)se(p##__DOT__##d_dly2_re, 17), \
    (long long)se(p##__DOT__##d_dly2_im, 17), \
    (long long)se(p##__DOT__##bfly_d_re, 17), \
    (long long)se(p##__DOT__##bfly_s_re, 17), \
    (long long)se(p##__DOT__##a_dly_re, 17), \
    (long long)se(p##__DOT__##tw_d1_re, 18), \
    (long long)se(p##__DOT__##mreg_re, 40), \
    (long long)se(p##__DOT__##mreg_im, 40), \
    (long long)se(p##__DOT__##comb_s_re, 43), \
    (long long)se(p##__DOT__##comb_s_im, 43), \
    (long long)se(p##__DOT__##comb_p_re, 43), \
    (long long)se(p##__DOT__##comb_p_im, 43))

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Vfft_sdf* d = new Vfft_sdf;
    d->rst = 1; d->ce = 0; d->s_axis_tvalid = 0;
    for (int i = 0; i < 6; i++) { d->clk=!d->clk; d->eval(); d->clk=!d->clk; d->eval(); }
    d->rst = 0;
    auto& r = *d->rootp;
    // read stimulus.txt: "<hex re> <hex im> <u> <l>" per line
    FILE* f = fopen("stimulus.txt", "r");
    if (!f) { printf("no stimulus.txt\n"); return 1; }
    std::vector<int> sv, su;
    unsigned rv, iv, uv, lv;
    while (fscanf(f, "%x %x %x %x", &rv, &iv, &uv, &lv) == 4) {
        sv.push_back((int)((rv << 16) | (iv & 0xFFFF)));
        su.push_back((int)((uv << 16) | (lv & 0xFFFF)));
    }
    fclose(f);
    printf("loaded %zu samples\n", sv.size());
    {
        d->clk = 1; d->eval();
        printf("RESET-DONE:\n");
        DUMP(0, 0, r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage);
        DUMP(0, 1, r.fft_sdf__DOT__stages__BRA__1__KET____DOT__u_stage);
        d->clk = 0; d->eval();
    }
    for (size_t c = 0; c < sv.size(); c++) {
        d->ce = 1; d->s_axis_tvalid = 1;
        d->s_axis_tdata_re = (sv[c] >> 16) & 0xFFFF;
        d->s_axis_tdata_im = sv[c] & 0xFFFF;
        d->s_axis_tuser = (su[c] >> 16) & 1;
        d->s_axis_tlast = su[c] & 1;
        d->clk = 1; d->eval();
        DUMP(c + 1, 0, r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage);
        DUMP(c + 1, 1, r.fft_sdf__DOT__stages__BRA__1__KET____DOT__u_stage);
        printf("   OUT valid=%d re=%d im=%d u=%d l=%d\n",
               (int)d->m_axis_tvalid, (int)d->m_axis_tdata_re,
               (int)d->m_axis_tdata_im, (int)d->m_axis_tuser,
               (int)d->m_axis_tlast);
        d->clk = 0; d->eval();
    }
    // drain: keep clocking, print only outputs
    for (int c = 0; c < 28; c++) {
        d->ce = 1; d->s_axis_tvalid = 1;
        d->s_axis_tdata_re = sv[c % sv.size()] >> 16 & 0xFFFF;
        d->s_axis_tdata_im = sv[c % sv.size()] & 0xFFFF;
        d->clk = 1; d->eval();
        printf("DRAIN c=%02d   OUT valid=%d re=%d im=%d\n",
               c + 1, (int)d->m_axis_tvalid,
               se((int)d->m_axis_tdata_re, 16), se((int)d->m_axis_tdata_im, 16));
        DUMP(c + 9, 0, r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage);
        DUMP3(c + 9, 0, r.fft_sdf__DOT__stages__BRA__0__KET____DOT__u_stage);
        d->clk = 0; d->eval();
    }
    return 0;
}
