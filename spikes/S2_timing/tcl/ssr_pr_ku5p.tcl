set outdir [lindex $argv 0]
open_checkpoint [file join $outdir synth.dcp]
opt_design
place_design
phys_opt_design
route_design
report_timing_summary -delay_type max -max_paths 10 -file [file join $outdir timing_pr.txt]
write_checkpoint -force [file join $outdir route.dcp]
