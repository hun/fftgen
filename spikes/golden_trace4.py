"""Golden stage-internals trace for N=4, matching spikes/dbg_trace4.cpp."""
from config import FFTConfig
from golden import SDFGoldenModel

cfg = FFTConfig(num_points=4)
m = SDFGoldenModel(cfg)
print("presets:", m.stage_presets, "preloads:", m.stage_preloads)
for t in range(1, 17):
    x = (1000 * ((t - 1) % 4 + 1), 0)
    print(f"--- step {t} in={x}")
    for g, st in enumerate(m.stages):
        print(f"pre  s{g} comp={int(st.in_compute)} i={st.i} w={st.wptr} "
              f"pw={st.pwp} ra={(st.wptr - st.D) % (2*st.D)} "
              f"pipe={''.join('1' if b else '0' for b in st.pipe_comp[:4])}")
    res = m.tick(True, x[0], x[1])
    for g, st in enumerate(m.stages):
        print(f"post s{g} comp={int(st.in_compute)} i={st.i} w={st.wptr} "
              f"pw={st.pwp} ra={(st.wptr - st.D) % (2*st.D)} "
              f"pipe={''.join('1' if b else '0' for b in st.pipe_comp[:4])} "
              f"d={st.d_reg} out=({st.out_reg[0]},{st.out_reg[1]})")
