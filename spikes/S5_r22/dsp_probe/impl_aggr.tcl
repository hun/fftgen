# P7 step 7c: aggressive post-route attempt on the exported r22 core.
# usage: vivado -mode batch -source impl_aggr.tcl -tclargs <part> <clk_mhz>
set part    [lindex $argv 0]
set clk_mhz [lindex $argv 1]
set rtl_dir [file normalize [file join [file dirname [info script]] ..]]
set period_ns [expr {1000.0 / $clk_mhz}]
file copy -force [file join $rtl_dir "fft_twiddles_r22.mem"] .

create_project -in_memory -part $part
add_files -fileset sources_1 [list \
    [file join $rtl_dir fft_core.v] \
    [file join $rtl_dir fft_sdf_r22.v] \
    [file join $rtl_dir fft_stage_r22.v] \
    [file join $rtl_dir fft_sdf.v]
]
set_property top fft_top [current_fileset]
synth_design -top fft_top
create_clock -period $period_ns -name clk [get_ports clk]

opt_design
place_design -directive Explore
phys_opt_design -directive AggressiveExplore
route_design -directive AggressiveExplore
phys_opt_design -directive AggressiveExplore
route_design
report_timing_summary -delay_type max -max_paths 10 -file timing_aggr.rpt
report_utilization -file utilization_aggr.rpt
exit
