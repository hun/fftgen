# S1: OOC synthesis of one coding on one part, utilization report out.
set part    [lindex $argv 0]
set rtl     [lindex $argv 1]
set top     [lindex $argv 2]
set outdir  [lindex $argv 3]
set depth   [lindex $argv 4]
set width   [lindex $argv 5]

file mkdir $outdir

create_project -in_memory -part $part
add_files -fileset sources_1 $rtl
set_property top $top [current_fileset]

synth_design -top $top \
    -generic DEPTH=$depth -generic WIDTH=$width \
    -flatten_hierarchy none

report_utilization -file [file join $outdir util.txt]

# extract the numbers we care about into a one-line summary
set fh [open [file join $outdir util.txt] r]
set summary ""
foreach line [split [read $fh] "\n"] {
    foreach pat {"LUT as Memory" "Block RAM Tile" "Block RAM Tile  " "RAMB36" "RAMB18" "URAM"} {
        if {[string first $pat $line] >= 0 && [regexp {\|} $line]} {
            append summary "$line\n"
        }
    }
}
close $fh
set sf [open [file join $outdir summary.txt] w]
puts $sf $summary
close $sf
