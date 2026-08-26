# SSR timing-vehicle P&R, high-effort directives (500 MHz marginal paths).
set outdir [lindex $argv 0]
open_checkpoint [file join $outdir synth.dcp]
opt_design
place_design -directive ExtraTimingOpt
phys_opt_design -directive AggressiveExplore
route_design -directive AggressiveExplore
phys_opt_design -directive AggressiveExplore
route_design -directive AggressiveExplore
report_timing_summary -delay_type max -max_paths 10 -file [file join $outdir timing_pr.txt]
write_checkpoint -force [file join $outdir route.dcp]
