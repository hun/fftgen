create_project -in_memory -part xcku5p-ffva676-1-e
read_verilog rot45_probe.v
synth_design -top rot45_probe_top -part xcku5p-ffva676-1-e
create_clock -period 2.000 -name clk [get_ports clk]
report_utilization -file utilization.rpt
report_timing_summary -delay_type max -max_paths 8 -nworst 2 -file timing.rpt
puts "DSP48: [llength [get_cells -hier -filter {REF_NAME == DSP48E2}]]"
puts "CARRY8: [llength [get_cells -hier -filter {REF_NAME == CARRY8}]]"
exit
