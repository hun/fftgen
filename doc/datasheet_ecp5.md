# fftgen datasheet -- ECP5 (Lattice Diamond 3.14 LSE)

None, synthesis only (no P&R timing), 16-bit samples / 18-bit twiddles (Q17), auto scaling, PIPE_DEPTH=10. DSP counts are inferred multipliers (all multiplies on DSP blocks, none in fabric). Sweep generated 2026-08-29.

| N | R | arch | DSP | LUT4 | regs | EBR |
|---|---|---|---|---|---|---|
| 16 | 1 | r2 | 8 | 2771 | 1975 | 0 |
| 16 | 1 | r22 | 8 | 1871 | 2247 | 0 |
| 64 | 1 | r2 | 16 | 4528 | 3243 | 4 |
| 64 | 1 | r22 | 12 | 3379 | 3508 | 1 |
| 128 | 2 | r2 | 36 | 11014 | 7309 | 8 |
| 128 | 2 | r22 | 28 | 8773 | 7987 | 2 |
| 256 | 1 | r2 | 24 | 6776 | 4474 | 8 |
| 256 | 1 | r22 | 16 | 6302 | 5388 | 5 |

