"""Golden stage-internals trace matching spikes/dbg_trace4.cpp (DIT-capable)."""
import sys
import dataclasses
from config import FFTConfig
from golden import SDFGoldenModel

# usage: golden_trace4.py <N> <dit 0/1>
N = int(sys.argv[1]) if len(sys.argv) > 1 else 4
dit = bool(int(sys.argv[2])) if len(sys.argv) > 2 else False
cfg = FFTConfig(num_points=N)
if dit:
    nat_in, nat_out = "bitreversed", "native"
else:
    nat_in, nat_out = "native", "bitreversed"
gm_cfg = dataclasses.replace(cfg, input_order=nat_in, output_order=nat_out)
m = SDFGoldenModel(gm_cfg, dit=dit)
print("presets:", m.stage_presets)
for i, pl in enumerate(m.stage_preloads):
    print(f"preload s{i}: {pl}")
