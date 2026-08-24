"""Cycle-accurate golden trace of stage internals, mirroring dbg_trace4.cpp.

usage: golden_trace_full.py <N> <dit 0/1> [stimulus.txt]
Single pass: one tick per stimulus line, prints post-tick stage state.
"""
import sys
import dataclasses
from config import FFTConfig
from golden import SDFGoldenModel

N = int(sys.argv[1])
dit = bool(int(sys.argv[2]))
stim_file = sys.argv[3] if len(sys.argv) > 3 else "stimulus.txt"

if dit:
    nat_in, nat_out = "bitreversed", "native"
else:
    nat_in, nat_out = "native", "bitreversed"
gm_cfg = dataclasses.replace(FFTConfig(num_points=N),
                             input_order=nat_in, output_order=nat_out)
m = SDFGoldenModel(gm_cfg, dit=dit)

samples = []
with open(stim_file) as f:
    for ln in f:
        p = ln.split()
        if len(p) == 4:
            samples.append((int(p[0], 16), int(p[1], 16)))

for t, (re, im) in enumerate(samples, start=1):
    res = m.tick(True, re, im)
    for g, st in enumerate(m.stages):
        adly = getattr(st, "a_dly", None)
        print(f"c={t:02d} s{g} pipe="
              f"{''.join('1' if b else '0' for b in st.pipe_comp[:4])} "
              f"d={st.d_reg} bfls={st.bfly_sum} "
              f"adly={adly if adly else '-'} mreg={st.mreg} "
              f"combS={st.comb_sum}")
    print(f"   OUT res={res}")

# drain
for c in range(28):
    re, im = samples[c % len(samples)]
    res = m.tick(True, re, im)
    st0 = m.stages[0]
    print(f"DRAIN g c={c+1:02d}   OUT valid={int(res[0])} re={res[1]} im={res[2]}")
