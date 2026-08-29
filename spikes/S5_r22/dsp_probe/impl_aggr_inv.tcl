# P8 4b: aggressive post-route attempt on the corner-inverse export.
# usage: vivado -mode batch -source impl_aggr_inv.tcl -tclargs <part> <clk_mhz>
set part    [lindex $argv 0]
set clk_mhz [lindex $argv 1]
set rtl_dir [file normalize [file join [file dirname [info script]] ..]]
set period_ns [expr {1000.0 / $clk_mhz}]
file copy -force [file join $rtl_dir "fft_w1_inv.mem"] .
file copy -force [file join $rtl_dir "fft_twiddles_r22_lane.mem"] .

create_project -in_memory -part $part
add_files -fileset sources_1 [list \
    [file join $rtl_dir fft_ssr_r22_inv.v] \
    [file join $rtl_dir fft_top_r22.v] \
    [file join $rtl_dir fft_sdf_r22.v] \
    [file join $rtl_dir fft_stage_r22.v] \
    [file join $rtl_dir fft_sdf.v] \
    [file join $rtl_dir fft_reorder.v]
]
set_property top fft_ssr_r22_inv [current_fileset]
synth_design -top fft_ssr_r22_inv \
    -generic NUM_POINTS=2048 -generic SSR=2 \
    -generic W1_FILE=fft_w1_inv.mem \
    -generic LANE_TW_FILE=fft_twiddles_r22_lane.mem \
    -generic SAMPLE_WIDTH=16 -generic SAMPLE_DECIMAL=0 \
    -generic OUTPUT_WIDTH=16 -generic OUTPUT_DECIMAL=0 \
    -generic TWIDDLE_WIDTH=18 -generic TWIDDLE_DECIMAL=17 \
    -generic SCALING_PACK=32'h00055555 -generic INTERN_WIDTH=17 \
    -generic PIPE_DEPTH=10 -generic INVERSE=1
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
