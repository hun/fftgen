# fftgen datasheet -- ECP5 (Lattice Diamond 3.14 LSE)

LFE5U-85F-8BG756C, synthesis only (no P&R timing), 16-bit samples / 18-bit twiddles (Q17), auto scaling, PIPE_DEPTH=10. DSP counts are inferred multipliers (all multiplies on DSP blocks, none in fabric). Sweep generated 2026-08-29.

| N | R | arch | DSP | LUT4 | regs | EBR |
|---|---|---|---|---|---|---|
| 16 | 1 | r2 | 8 | 2771 | 1975 | 0 |
| 64 | 1 | r2 | 16 | 4528 | 3243 | 4 |
| 256 | 1 | r2 | 24 | 6776 | 4474 | 8 |
| 128 | 2 | r2 | 36 | 11014 | 7309 | 8 |

