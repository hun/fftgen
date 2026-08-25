# SSR timing-vehicle OOC synthesis on KU5P: ssr_timing_wrap (internal
# stimulus/sink, so only {clk,rst,done} on the boundary).
set part    [lindex $argv 0]
set npts    [lindex $argv 1]
set ssr     [lindex $argv 2]
set pack    [lindex $argv 3]
set intern  [lindex $argv 4]
set outdir  [lindex $argv 5]

file mkdir $outdir
create_project -in_memory -part $part
add_files -fileset sources_1 [list \
    /home/hannes/Projects/fftgen/spikes/S2_timing/rtl/ssr_timing_wrap.v \
    /home/hannes/Projects/fftgen/rtl/fft_ssr.v \
    /home/hannes/Projects/fftgen/rtl/fft_top.v \
    /home/hannes/Projects/fftgen/rtl/fft_sdf.v \
    /home/hannes/Projects/fftgen/rtl/fft_reorder.v \
    /home/hannes/Projects/fftgen/rtl/fft_cross.v ]
set_property top ssr_timing_wrap [current_fileset]
set_property verilog_define {FFTGEN_PRELOADS=1} [current_fileset]
set_property include_dirs $outdir [current_fileset]
set wn_abs  [file normalize [file join [pwd] fft_wn.mem]]
set tw_abs [file normalize [file join [pwd] fft_twiddles_lane.mem]]
synth_design -top ssr_timing_wrap \
    -generic NUM_POINTS=$npts -generic SSR=$ssr \
    -generic SAMPLE_WIDTH=16 -generic SAMPLE_DECIMAL=0 \
    -generic OUTPUT_WIDTH=16 -generic OUTPUT_DECIMAL=0 \
    -generic TWIDDLE_WIDTH=18 -generic TWIDDLE_DECIMAL=17 \
    -generic SCALING_PACK=$pack -generic INTERN_WIDTH=$intern
create_clock -period 2.0 -name clk [get_ports clk]
report_utilization -file [file join $outdir util.txt]
report_timing_summary -delay_type max -max_paths 10 -file [file join $outdir timing.txt]
write_checkpoint -force [file join $outdir synth.dcp]
