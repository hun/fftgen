# fftgen datasheet -- Cyclone V (Quartus 25.1 Lite)

None, synthesis only (no P&R timing), 16-bit samples / 18-bit twiddles (Q17), auto scaling, PIPE_DEPTH=10. DSP counts are inferred multipliers (all multiplies on DSP blocks, none in fabric). Sweep generated 2026-08-29.

| N | R | arch | DSP | ALM | regs | M10K |
|---|---|---|---|---|---|---|
| 16 | 1 | r2 | 8 | 1,668 | 4419 | 4 |
| 16 | 1 | r22 | 8 | 1,312 | 3037 | 1 |
| 64 | 1 | r2 | 16 | 3,853 | 9452 | 6 |
| 64 | 1 | r22 | 12 | 3,671 | 7826 | 1 |
| 128 | 2 | r2 | 36 | 12,575 | 28338 | 14 |
| 128 | 2 | r22 | 28 | 12,055 | 25000 | 2 |
| 256 | 1 | r2 | 24 | 10,972 | 24432 | 24 |
| 256 | 1 | r22 | 16 | 11,468 | 23756 | 5 |

