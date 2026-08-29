#include <cstdio>
#include "Vqs.h"
#include "verilated.h"
int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Vqs* d = new Vqs;
    d->eval();
    printf("A=%lld B=%lld C=%lld D=%lld E=%lld  (true=%lld)\n",
           (long long)d->o_A, (long long)d->o_B, (long long)d->o_C,
           (long long)d->o_D, (long long)d->o_E,
           (long long)(-7625LL * 131071LL));
    return 0;
}
