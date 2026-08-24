set part   [lindex $argv 0]
set outdir [lindex $argv 1]
file mkdir $outdir
create_project -in_memory -part $part
add_files -fileset sources_1 /home/hannes/Projects/fftgen/spikes/S3_dsp_cascade/rtl/dsp_cascade_v.v
set_property top dsp_cascade_v [current_fileset]
synth_design -top dsp_cascade_v
create_clock -period 2.0 -name clk [get_ports clk]

set fh [open [file join $outdir report.txt] w]
puts $fh "DSPs: [llength [get_cells -hier -filter {REF_NAME =~ DSP48E2}]]"
foreach c [get_cells -hier -filter {REF_NAME =~ DSP48E2}] {
    puts $fh "--- [get_property NAME $c] PREG=[get_property PREG $c] MREG=[get_property MREG $c]"
    # cascade sinks of my PCOUT
    set pco [get_pins -quiet -of $c -filter "REF_PIN_NAME =~ PCOUT*"]
    set sinks {}
    foreach p $pco { foreach n [get_nets -quiet -of $p] {
        foreach s [get_pins -quiet -of $n -filter {REF_PIN_NAME =~ PCIN*}] {
            lappend sinks [get_property PARENT_CELL $s] } } }
    set sinks [lsort -unique $sinks]
    puts $fh "    PCOUT -> [expr {[llength $sinks] ? $sinks : "(none)"}]"
    # who feeds my PCIN
    set pci [get_pins -quiet -of $c -filter {REF_PIN_NAME =~ PCIN*}]
    set srcs {}
    foreach p $pci { foreach n [get_nets -quiet -of $p] {
        foreach s [get_pins -quiet -leaf -of $n -filter {DIRECTION == OUT && REF_PIN_NAME =~ PCOUT*}] {
            lappend srcs [get_property PARENT_CELL $s] } } }
    set srcs [lsort -unique $srcs]
    puts $fh "    PCIN <- [expr {[llength $srcs] ? $srcs : "(none)"}]"
}
close $fh
report_utilization -file [file join $outdir util.txt]
