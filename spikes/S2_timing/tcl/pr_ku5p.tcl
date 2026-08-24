# S2c: place & route the N=64 KU5P OOC synth checkpoint at 2 ns.
set outdir [lindex $argv 0]

open_checkpoint [file join $outdir synth.dcp]
puts "constraints: [get_clocks]"
report_timing_summary -delay_type max -max_paths 3 \
    -file [file join $outdir timing_post_synth.txt]

opt_design
place_design
phys_opt_design
route_design

report_timing_summary -delay_type max -max_paths 10 \
    -file [file join $outdir timing_pr.txt]
report_utilization -file [file join $outdir util_pr.txt]
report_route_status -file [file join $outdir route_status.txt]
write_checkpoint -force [file join $outdir route.dcp]
