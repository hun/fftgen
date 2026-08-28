"""Build the fft_cross timing variants from rtl/fft_cross.v.

  nat   -- natural-width products (stored at AW) + a plain same-width
           combine, NO structural change (isolates the ext() glue lever)
  stg   -- nat + the early-hop im/re stagger with C-port re-alignment
           regs (the fft_stage_r22 recipe; latency-neutral)
  stge  -- stg but with the old ext() glue reinstated at the combine
           (isolates the stagger lever)

All variants keep the pipeline edges identical, so CB_LAT / the pd taps
are unchanged (verified by the RTL-vs-golden SSR suites).

Run: python3 spikes/S5_r22/dsp_probe/mk_xbar_variants.py
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
OUT = os.path.join(HERE, "variants_xbar")
head = open(os.path.join(ROOT, "rtl", "fft_cross.v")).read()

PW0, PW1 = "{PW{1'b0}}", "{AW{1'b0}}"

DECL_OLD = """    reg signed [PW-1:0] pp1 [0:R-1];
    reg signed [PW-1:0] pp2 [0:R-1];
    reg signed [PW-1:0] pp3 [0:R-1];
    reg signed [PW-1:0] pp4 [0:R-1];"""
DECL_NEW = """    reg signed [AW-1:0] pp1 [0:R-1];
    reg signed [AW-1:0] pp2 [0:R-1];
    // the IM-operand products ride one DSP hop EARLY (from q/wa -- the
    // same value as d/wq one clock later) and are re-aligned to the
    // re-path products by the C-port regs below, so the combine sees a
    // matched MREG/C-port pair and the DSP keeps its MREG. Same two
    // levers as fft_stage_r22 (see its header): without them Vivado
    // merges the combine into the multiply's cycle (1.85 ns intra-DSP).
    reg signed [AW-1:0] pp3 [0:R-1];
    reg signed [AW-1:0] pp4 [0:R-1];
    reg signed [AW-1:0] pc3 [0:R-1];
    reg signed [AW-1:0] pc4 [0:R-1];"""

PROD_OLD = """                    if (gp == 0) begin
                        // W_0 = (1, 0) in Q(td): pp1 = re<<td,
                        // pp3 = im<<td, pp2 = pp4 = 0
                        pp1[gp] <= d_re[gp] <<< TWIDDLE_DECIMAL;
                        pp2[gp] <= {PW{1'b0}};
                        pp3[gp] <= d_im[gp] <<< TWIDDLE_DECIMAL;
                        pp4[gp] <= {PW{1'b0}};
                    end else begin
                        pp1[gp] <= $signed(d_re[gp]) * wq_re[gp];
                        pp2[gp] <= $signed(d_re[gp]) * wq_im[gp];
                        pp3[gp] <= $signed(d_im[gp]) * wq_re[gp];
                        pp4[gp] <= $signed(d_im[gp]) * wq_im[gp];
                    end"""
PROD_NEW = """                    // im-operand products from the EARLIER operand hop
                    if (gp == 0) begin
                        pp3[gp] <= q_im[gp] <<< TWIDDLE_DECIMAL;
                        pp4[gp] <= {AW{1'b0}};
                    end else begin
                        pp3[gp] <= q_im[gp] * wa_re[gp];
                        pp4[gp] <= q_im[gp] * wa_im[gp];
                    end
                    // re-operand products (MREG) + the im products at the
                    // combine DSPs' C ports (CREG)
                    if (gp == 0) begin
                        pp1[gp] <= d_re[gp] <<< TWIDDLE_DECIMAL;
                        pp2[gp] <= {AW{1'b0}};
                    end else begin
                        pp1[gp] <= d_re[gp] * wq_re[gp];
                        pp2[gp] <= d_re[gp] * wq_im[gp];
                    end
                    pc3[gp] <= pp3[gp];
                    pc4[gp] <= pp4[gp];"""
PROD_NAT = PROD_NEW  # nat keeps d/wq for all four products
PROD_NAT = """                    if (gp == 0) begin
                        // W_0 = (1, 0) in Q(td): pp1 = re<<td,
                        // pp3 = im<<td, pp2 = pp4 = 0
                        pp1[gp] <= d_re[gp] <<< TWIDDLE_DECIMAL;
                        pp2[gp] <= {AW{1'b0}};
                        pp3[gp] <= d_im[gp] <<< TWIDDLE_DECIMAL;
                        pp4[gp] <= {AW{1'b0}};
                    end else begin
                        pp1[gp] <= d_re[gp] * wq_re[gp];
                        pp2[gp] <= d_re[gp] * wq_im[gp];
                        pp3[gp] <= d_im[gp] * wq_re[gp];
                        pp4[gp] <= d_im[gp] * wq_im[gp];
                    end"""

COMB_OLD = """                b_re[i] <= $signed(ext(pp1[i])) - $signed(ext(pp4[i]));
                b_im[i] <= $signed(ext(pp2[i])) + $signed(ext(pp3[i]));"""
COMB_NEW = """                b_re[i] <= pp1[i] - pc4[i];
                b_im[i] <= pp2[i] + pc3[i];"""
COMB_EXT = """                b_re[i] <= $signed(ext(pp1[i])) - $signed(ext(pc4[i]));
                b_im[i] <= $signed(ext(pp2[i])) + $signed(ext(pc3[i]));"""
COMB_NAT = """                b_re[i] <= pp1[i] - pp4[i];
                b_im[i] <= pp2[i] + pp3[i];"""

# NOTE: pp*/pc* have no reset branch in this design (as before)


def build(decl, prod, comb, drop_ab_ext):
    s = head
    for old, new in ((DECL_OLD, decl), (PROD_OLD, prod),
                     (COMB_OLD, comb)):
        assert old in s, old[:48]
        s = s.replace(old, new)
    if drop_ab_ext:
        # b_* and h_* are already AW-wide; ext()'s PW-wide input port
        # TRUNCATES them (harmless at 16b, wrong at wide widths)
        s = re.sub(r"ext\((b_[reim]\[[^\]]+\])\)", r"\1", s)
        s = re.sub(r"ext\((h_[reim]\[[^\]]+\])\)", r"\1", s)
    return s


DECL_NAT = """    reg signed [AW-1:0] pp1 [0:R-1];
    reg signed [AW-1:0] pp2 [0:R-1];
    reg signed [AW-1:0] pp3 [0:R-1];
    reg signed [AW-1:0] pp4 [0:R-1];"""
RST_NAT = """            pp1[i] <= {AW{1'b0}};  pp2[i] <= {AW{1'b0}};
            pp3[i] <= {AW{1'b0}};  pp4[i] <= {AW{1'b0}};"""

variants = {
    # width/coding lever only: products stored at AW, plain combine
    "nat": build(DECL_NAT, PROD_NAT, COMB_NAT, True),
    # both levers (the fft_stage_r22 recipe)
    "stg": build(DECL_NEW, PROD_NEW, COMB_NEW, True),
    # stagger only, with the old ext() glue reinstated at the combine
    "stge": build(DECL_NEW, PROD_NEW, COMB_EXT, True),
}
os.makedirs(OUT, exist_ok=True)
for name, txt in variants.items():
    open(os.path.join(OUT, f"fft_cross_{name}.v"), "w").write(txt)
    print(f"fft_cross_{name}.v written")
