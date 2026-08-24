# DSP register audit: how are AREG/BREG/MREG/PREG used in the netlist?
set dcp [lindex $argv 0]
open_checkpoint $dcp

set n [llength [get_cells -hier -filter {PRIMITIVE_TYPE =~ *DSP*}]]
puts "DSP instances: $n"

# for a sample of DSPs, report the register mode (AREG/BREG/MREG/PREG)
# and whether A/B inputs come from registers
set dsps [get_cells -hier -filter {PRIMITIVE_TYPE =~ *DSP*}]
set seen 0
foreach dsp $dsps {
    if {$seen >= 4} break
    incr seen
    puts "--- $dsp ---"
    # source of the B1/B2 inputs: register or combinational?
    set bnet [get_nets -of_objects [get_pins -of_objects [get_cells $dsp] -filter {NAME =~ */B1}]]
    set bdrv [get_drivers -quiet -of_objects $bnet]
    puts "  B1 driver: $bdrv"
    set a1net [get_nets -of_objects [get_pins -of_objects [get_cells $dsp] -filter {NAME =~ */A1}]]
    set a1drv [get_drivers -quiet -of_objects $a1net]
    puts "  A1 driver: $a1drv"
    # where does P go?
    set pnet [get_nets -of_objects [get_pins -of_objects [get_cells $dsp] -filter {NAME =~ */P}]]
    set pld [get_sinks -quiet -of_objects $pnet]
    puts "  P load: $pld"
}
