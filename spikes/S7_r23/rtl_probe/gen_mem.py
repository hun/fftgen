"""Twiddle ROM for the r23 stage probe: the interleaved layout the stage
reads -- word[base_k + g] = T[k*g*8^m] with base_k = the pfifo class
bases (y2->0, y6->G, idle 2G, y1->3G, y5->4G, y3->5G, y7->6G, y4->7G)."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from twiddles import canonical_twiddles

N, G, tw, td = 1024, 128, 18, 17
t = canonical_twiddles(N, tw, td, inverse=False)
layout = [(0, 2), (G, 6), (3 * G, 1), (4 * G, 5),
          (5 * G, 3), (6 * G, 7), (7 * G, 4)]
words = [0] * (8 * G)
mask = (1 << tw) - 1
for base, k in layout:
    for g in range(G):
        re, im = t[(k * g) % N]
        words[base + g] = ((re & mask) << tw) | (im & mask)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "build_probe", "fft_twiddles_r23.mem")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    for w in words:
        f.write("%05x\n" % w)
print("wrote", out, len(words), "words")
