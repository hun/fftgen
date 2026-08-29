# fftgen -- cross-vendor synthesis targets.
#
# One export (src/export_core) is synthesized by each vendor flow and the
# resource summary printed. The datasheet-* targets run a sparse sweep
# across N and write doc/datasheet_<vendor>.md.
#
#   make synth-xilinx N=64              # KU5P OOC (defaults)
#   make synth-intel  N=256             # Cyclone V
#   make synth-lattice SSR=2            # ECP5
#   make synth-all
#   make datasheet-intel datasheet-lattice datasheet-vivado
#
# Tool paths override via VIVADO_BIN / QUARTUS_BIN / DIAMOND_ROOT.

VIVADO_BIN  ?= /tools/Xilinx/2026.1/Vivado/bin/vivado
QUARTUS_BIN ?= /tools/Altera/altera_lite/25.1std/quartus/bin
DIAMOND_ROOT?= /tools/lscc/diamond/3.14

PART_KU5P    ?= xcku5p-ffva676-1-e
PART_CYCLONE5 ?= 5CEBA7F23C7
PART_ECP5    ?= LFE5U-85F-8BG756C

N      ?= 64
SSR    ?= 1
ARCH   ?= r2
CLK_MHZ ?= 500

SYNTH  ?= build/synth

export QUARTUS_BIN DIAMOND_ROOT

# ----------------------------------------------------------------------
# export helper: one configuration -> build/synth/export_<N>_R<SSR>_<ARCH>
# ----------------------------------------------------------------------

EXPORT_DIR = $(SYNTH)/export_$(N)_R$(SSR)_$(ARCH)

.PHONY: export
export:
	mkdir -p $(SYNTH)
	python3 -m src.export_core --num-points $(N) \
	  $(if $(filter-out 1,$(SSR)),--ssr $(SSR) --output-order native,) \
	  --stage-mode $(ARCH) --clk-mhz $(CLK_MHZ) --outdir $(EXPORT_DIR)

# ----------------------------------------------------------------------
# single-config synthesis targets
# ----------------------------------------------------------------------

.PHONY: synth-xilinx synth-intel synth-lattice synth-all

synth-xilinx: export
	cd $(EXPORT_DIR)/vivado && $(VIVADO_BIN) -mode batch -source synth.tcl \
	  -tclargs $(PART_KU5P) $(CLK_MHZ) 2>&1 | tee ../vivado_synth.log | \
	  grep -E "LUTs|Registers|DSPs|BRAM|WNS|URGENT" || true

synth-intel: export
	QUARTUS_BIN=$(QUARTUS_BIN) python3 -m src.synth_vendor intel \
	  $(EXPORT_DIR) $(PART_CYCLONE5)

synth-lattice: export
	DIAMOND_ROOT=$(DIAMOND_ROOT) python3 -m src.synth_vendor lattice \
	  $(EXPORT_DIR) $(PART_ECP5)

synth-all: synth-xilinx synth-intel synth-lattice

# ----------------------------------------------------------------------
# sparse datasheet sweeps (N in {16,64,256}, r2 R=1; + one SSR row)
# ----------------------------------------------------------------------

VENDOR_SIZES = 16 64 256

.PHONY: datasheet-intel datasheet-lattice datasheet-vivado

datasheet-intel:
	QUARTUS_BIN=$(QUARTUS_BIN) python3 -m src.datasheet_sweep_vendor \
	  intel doc/datasheet_cyclone5.md --part $(PART_CYCLONE5) \
	  --sizes $(VENDOR_SIZES)

datasheet-lattice:
	DIAMOND_ROOT=$(DIAMOND_ROOT) python3 -m src.datasheet_sweep_vendor \
	  lattice doc/datasheet_ecp5.md --part $(PART_ECP5) \
	  --sizes $(VENDOR_SIZES)

datasheet-vivado:
	VIVADO_BIN=$(VIVADO_BIN) python3 -m src.datasheet_sweep \
	  --r1 64 --arch r2 -j 2 --jobs-dir $(SYNTH)/vivado_ds
