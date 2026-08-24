# S3 spike: synth the MAC chain, then probe DSP48E2 instances for
# PCOUT->PCIN cascade connectivity and pipeline register properties.
set part   [lindex $argv 0]
set outdir [lindex $argv 1]
set src    [lindex $argv 2]

file mkdir $outdir
create_project -in_memory -part $part
add_files -fileset sources_1 $src
set_property top dsp_cascade [current_fileset]
set_property file_type {VHDL 2008} [get_files $src]
synth_design -top dsp_cascade
create_clock -period 2.0 -name clk [get_ports clk]

set fh [open [file join $outdir report.txt] w]

set cells [get_cells -hier -filter {REF_NAME =~ DSP48E2}]
puts $fh "DSP48E2 instances: [llength $cells]"

foreach c $cells {
    set nm [get_property NAME $c]
    puts $fh "--- $nm"
    foreach prop {AREG BREG DREG MREG PREG USE_DPORT PREADDINSEL} {
        set v [get_property -quiet $prop $c]
        puts $fh "    $prop = $v"
    }
    # does this instance drive PCOUT into another DSP's PCIN?
    set pco [get_nets -quiet -of [get_pins -of $c -filter {REF_PIN_NAME == "PCOUT"}]]
    if {[llength $pco]} {
        set sinks [get_pins -quiet -of $pco -filter {REF_PIN_NAME == "PCIN"}]
        foreach s $sinks {
            puts $fh "    PCOUT -> [get_property PARENT_CELL $s]/PCIN"
        }
    } else {
        puts $fh "    PCOUT: unconnected"
    }
}

close $fh
report_utilization -file [file join $outdir util.txt]
write_checkpoint -force [file join $outdir synth.dcp]
