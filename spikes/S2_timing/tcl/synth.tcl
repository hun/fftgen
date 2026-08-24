# S2: OOC synthesis + timing estimate of fft_sdf on UltraScale+.
# usage: synth_design with -generic parameter overrides, 500 MHz target.
set part           [lindex $argv 0]
set npts           [lindex $argv 1]
set sample_w       [lindex $argv 2]
set twiddle_w      [lindex $argv 3]
set scaling_pack   [lindex $argv 4]
set intern_w       [lindex $argv 5]
set outdir         [lindex $argv 6]
set clk_ns         [lindex $argv 7]

file mkdir $outdir

create_project -in_memory -part $part
add_files -fileset sources_1 /home/hannes/Projects/fftgen/rtl/fft_sdf.v
set_property top fft_sdf [current_fileset]

synth_design -top fft_sdf \
    -generic NUM_POINTS=$npts \
    -generic SAMPLE_WIDTH=$sample_w \
    -generic TWIDDLE_WIDTH=$twiddle_w \
    -generic SCALING_PACK=$scaling_pack \
    -generic INTERN_WIDTH=$intern_w \
    -generic TWIDDLE_FILE="fft_twiddles.mem" \
    -generic OUTPUT_WIDTH=$sample_w \
    -generic OUTPUT_DECIMAL=0 \
    -generic SAMPLE_DECIMAL=0 \
    -generic TWIDDLE_DECIMAL=[expr $twiddle_w - 1]

# 500 MHz target (2.0 ns) unless overridden
set period_ns $clk_ns
create_clock -period $period_ns -name clk [get_ports clk]

report_utilization -file [file join $outdir util.txt]
report_timing_summary -delay_type max -max_paths 15 \
    -file [file join $outdir timing.txt]
report_timing -delay_type max -max_paths 5 -path_type full \
    -file [file join $outdir crit.txt]

# condensed summary
set fh [open [file join $outdir summary.txt] w]
puts $fh "PART=$part N=$npts SW=$sample_w TW=$twiddle_w PERIOD=$period_ns"
set f [open [file join $outdir util.txt] r]
while {[gets $f line] >= 0} {
    if {[regexp {LUT as Memory|Block RAM Tile|DSPs |URAM } $line]} { puts $fh $line }
}
close $f
set f [open [file join $outdir timing.txt] r]
while {[gets $f line] >= 0} {
    if {[regexp {^\s*clk\s+-|WNS} $line]} { puts $fh $line }
}
close $f
close $fh
