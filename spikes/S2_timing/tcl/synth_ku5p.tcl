# S2b: synth on KU5P, save checkpoint, report DSP register usage.
set part    [lindex $argv 0]
set npts    [lindex $argv 1]
set pack    [lindex $argv 2]
set intern  [lindex $argv 3]
set outdir  [lindex $argv 4]

file mkdir $outdir
create_project -in_memory -part $part
add_files -fileset sources_1 /home/hannes/Projects/fftgen/rtl/fft_sdf.v
set_property top fft_sdf [current_fileset]
synth_design -top fft_sdf \
    -generic NUM_POINTS=$npts -generic SAMPLE_WIDTH=16 -generic TWIDDLE_WIDTH=18 \
    -generic SCALING_PACK=$pack -generic INTERN_WIDTH=$intern \
    -generic TOPOLOGY=0
create_clock -period 2.0 -name clk [get_ports clk]
report_utilization -file [file join $outdir util.txt]
report_timing_summary -delay_type max -max_paths 10 -file [file join $outdir timing.txt]
write_checkpoint -force [file join $outdir synth.dcp]
