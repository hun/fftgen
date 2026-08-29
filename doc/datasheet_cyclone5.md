# fftgen datasheet -- Cyclone V (Quartus 25.1 Lite)

5CEBA7F23C7, synthesis only (no P&R timing), 16-bit samples / 18-bit twiddles (Q17), auto scaling, PIPE_DEPTH=10. DSP counts are inferred multipliers (all multiplies on DSP blocks, none in fabric). Sweep generated 2026-08-29.

| N | R | arch | DSP | ALM | regs | M10K |
|---|---|---|---|---|---|---|
| 16 | 1 | r2 | 8 | 1,668 | 4419 | 4 |
| 64 | 1 | r2 | 16 | 3,853 | 9452 | 6 |
| 256 | 1 | r2 | 24 | 10,972 | 24432 | 24 |
| 128 | 2 | r2 | 36 | 12,575 | 28338 | 14 |

