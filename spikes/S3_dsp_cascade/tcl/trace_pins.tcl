# Trace how each DSP's product output reaches the next stage.
set outdir [lindex $argv 0]
set dcp    [lindex $argv 1]

open_checkpoint $dcp
set fh [open [file join $outdir trace.txt] w]

foreach c [get_cells -hier -filter {REF_NAME =~ DSP48E2}] {
    puts $fh "=== [get_property NAME $c]"
    foreach pin {P PCOUT PCIN C} {
        set pp [get_pins -quiet -of $c -filter "REF_PIN_NAME =~ \"${pin}*\""]
        if {[llength $pp]} {
            set nets {}
            foreach p $pp {
                foreach n [get_nets -quiet -of $p] { lappend nets $n }
            }
            set nets [lsort -unique $nets]
            if {[llength $nets]} {
                foreach n $nets {
                    set drv [get_pins -quiet -leaf -of $n -filter {DIRECTION == OUT && REF_PIN_NAME =~ "P*"}]
                    puts $fh "  $pin: net '$n' from $drv"
                }
            } else {
                puts $fh "  $pin: (no net)"
            }
        } else {
            puts $fh "  $pin: (pin absent)"
        }
    }
}
close $fh
