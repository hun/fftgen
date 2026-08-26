set dcp [lindex $argv 0]
set outdir [lindex $argv 1]
open_checkpoint $dcp
set fh [open [file join $outdir dsp_audit.txt] w]
foreach c [get_cells -hier -filter {REF_NAME =~ DSP48E2}] {
    set hier [get_property NAME $c]
    set a1 [get_property A1REG $c]; set a2 [get_property A2REG $c]
    set b1 [get_property B1REG $c]; set b2 [get_property B2REG $c]
    set d1 [get_property D1REG $c]; set d2 [get_property D2REG $c]
    set cr [get_property CREG $c]; set mr [get_property MREG $c]
    set pr [get_property PREG $c]; set ar [get_property ADREG $c]
    puts $fh "$hier : A1=$a1 A2=$a2 B1=$b1 B2=$b2 D1=$d1 D2=$d2 C=$cr M=$mr P=$pr AD=$ar"
}
close $fh
