#!/usr/bin/env python3
"""Export a complete fftgen core deliverable set (PLAN.md P5b).

Produces a self-contained directory for one configuration: RTL sources,
twiddle/preload artifacts, a GENERATED top-level wrapper with every
parameter baked in (no -G overrides needed anywhere), fft_params.vh,
human-readable params.txt / twiddle_map.txt, verification vectors
(stimulus + golden expected), and Vivado OOC scripts.

    python3 src/export_core.py --num-points 1024 --outdir build/export
    python3 src/export_core.py --num-points 8192 --ssr 2 \\
        --output-order native --outdir build/export_ssr

The exported tree is verified with the same bit-exact flow as the test
suite (see README.txt written alongside the files).
"""

import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from config import FFTConfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RTL = os.path.join(ROOT, "rtl")
TB = os.path.join(ROOT, "tb")

# memory-style cutoffs (doc/mem_cutoffs.md S3); mirrored from fft_stage
LUTRAM_MAX_BITS = 1024
BRAM_MAX_BITS = 262144


# ----------------------------------------------------------------------
# config helpers
# ----------------------------------------------------------------------

def build_config(args) -> FFTConfig:
    scaling = args.scaling
    if scaling != "auto":
        scaling = [int(x) for x in scaling.replace(",", " ").split()]
    return FFTConfig(
        num_points=args.num_points,
        inverse=args.inverse,
        ssr=args.ssr,
        input_order=args.input_order,
        output_order=args.output_order,
        sample_width=args.sample_width,
        sample_decimal=args.sample_decimal,
        output_width=args.output_width,
        output_decimal=args.output_decimal,
        twiddle_width=args.twiddle_width,
        scaling=scaling,
        stage_mode=args.stage_mode,
    )


def lane_cfg(cfg: FFTConfig) -> FFTConfig:
    """Per-lane engine config for SSR (M-point R=1 engine)."""
    import copy
    lc = copy.copy(cfg)
    lc.num_points = cfg.num_points // cfg.ssr
    lc.ssr = 1
    return lc


def intern_width(cfg: FFTConfig) -> int:
    if cfg.stage_mode == "r23":
        # the r23 datapath is verified at INTERN_WIDTH=16 (independent of
        # the r2/r22 LUTRAM-timing formula); see doc/datasheet.md r23 rows
        return 16
    return cfg.sample_width + max(0, cfg.num_stages - sum(cfg.shifts)) + 1
def bake_ssr_wrapper(cfg: FFTConfig, src_path: str, out_path: str) -> None:
    """Rewrite the SSR top's parameter defaults from ``cfg``.

    rtl/fft_ssr.v / fft_ssr_r22.v / fft_ssr_r22_inv.v are generic
    parameterized modules; the R=1 flow bakes every parameter into a
    generated fft_core.v, but the SSR export historically copied the
    generic tops and passed the values as Vivado ``-generic`` overrides
    (synth_design in vivado/synth.tcl). Any other synthesis flow then
    silently synthesized the DEFAULT size (e.g. N=8). Bake the values
    so every flow sees the configured core.
    """
    lc = lane_cfg(cfg)
    lc.input_order = "native"
    lc.output_order = "bitreversed"
    pack = 0
    for s_, sh in enumerate(lc.shifts):
        pack |= (sh & 3) << (2 * s_)
    subs = {
        "NUM_POINTS": cfg.num_points,
        "SSR": cfg.ssr,
        "SAMPLE_WIDTH": cfg.sample_width,
        "SAMPLE_DECIMAL": cfg.sample_decimal,
        "OUTPUT_WIDTH": cfg.output_width,
        "OUTPUT_DECIMAL": cfg.output_decimal,
        "TWIDDLE_WIDTH": cfg.twiddle_width,
        "TWIDDLE_DECIMAL": cfg.twiddle_decimal,
        "SCALING_PACK": "32'h%08x" % pack,
        "INTERN_WIDTH": intern_width(lc),
        "INVERSE": 1 if cfg.inverse else 0,
    }
    if cfg.is_r22:
        subs["REORDER_OUT"] = 0 if cfg.output_order == "bitreversed" else 1
    src = open(src_path).read()
    for name, value in subs.items():
        src = re.sub(
            r"(parameter\s+(?:integer\s+)?" + name + r"\s*=\s*)[^,)\n]+",
            r"\g<1>" + str(value), src, count=1)
    with open(out_path, "w") as f:
        f.write(src)



def scaling_pack(cfg: FFTConfig) -> int:
    pack = 0
    for s, sh in enumerate(cfg.shifts):
        pack |= (sh & 3) << (2 * s)
    return pack


def reorder_out(cfg: FFTConfig) -> bool:
    """Output reorder inserted when the outer order differs from the
    core's natural output order (bitreversed for DIF, native for DIT)."""
    core_out = "native" if cfg.is_dit else "bitreversed"
    return cfg.output_order != core_out


def mem_style_bits(bits: int) -> str:
    if bits <= LUTRAM_MAX_BITS:
        return "distributed"
    if bits < BRAM_MAX_BITS:
        return "block"
    return "ultra"


def core_files(cfg: FFTConfig):
    """RTL source files for this architecture (fft_core.v first so the
    preload define is parsed before fft_sdf.v in naive compile orders)."""
    if cfg.is_r22:
        # fft_sdf.v carries the plain fft_stage module used for the
        # odd-n leftover; NO fft_preloads.vh dependency (the r22
        # leftover preloads are parity-computed inside fft_sdf_r22.v)
        if cfg.input_order == "bitreversed":
            # P8 4b corner-order IFFT: transpose wrapper + lanes, no crossbar
            return ["fft_ssr_r22_inv.v", "fft_top_r22.v", "fft_sdf_r22.v",
                    "fft_stage_r22.v", "fft_sdf.v", "fft_reorder.v"]
        if cfg.ssr > 1:
            return ["fft_ssr_r22.v", "fft_top_r22.v", "fft_sdf_r22.v",
                    "fft_stage_r22.v", "fft_sdf.v", "fft_reorder.v",
                    "fft_cross.v"]
        return ["fft_sdf_r22.v", "fft_stage_r22.v", "fft_sdf.v"]
    if cfg.stage_mode == "r23":
        if cfg.ssr > 1:
            return ["fft_ssr_r23.v", "fft_sdf_r23.v", "fft_stage_r23.v",
                    "fft_stage_r22.v", "fft_cross.v"]
        return ["fft_sdf_r23.v", "fft_stage_r23.v", "fft_stage_r22.v"]
    if cfg.ssr > 1:
        return ["fft_ssr.v", "fft_top.v", "fft_sdf.v", "fft_reorder.v",
                "fft_cross.v"]
    return ["fft_sdf.v", "fft_reorder.v"]


def latency_cycles(cfg: FFTConfig) -> int:
    """Declared streaming latency in enabled cycles (from the golden
    models -- single source of truth)."""
    if cfg.is_r22:
        if cfg.input_order == "bitreversed":
            # P8 4b corner-order IFFT (bitrev -> native, R=2): the
            # transpose route -- wrapper (2) + M-point input reorder (M)
            # + the DIF-IDFT lane's own latency
            from golden_ssr import SSRCornerInverseModel
            return SSRCornerInverseModel(cfg, arch="r22").latency
        # verified RTL chain: sum(3D+9) per pair + 11 (odd-n leftover)
        # + 1 (registered quantizer output); SSR adds the lane reorder
        # M and the crossbar on top (mirrors golden_ssr's lane model)
        n = cfg.num_stages
        if cfg.ssr == 1:
            lat = sum(3 * (cfg.num_points >> (2 * m + 2)) + 9
                      for m in range(n // 2)) + (11 if n % 2 else 0) + 1
            return lat
        from golden_ssr import SSRGoldenModel
        return SSRGoldenModel(cfg, arch="r22").latency
    if cfg.stage_mode == "r23":
        # measured data latency: sum(7G+12) per triple + sum(3D+9) per
        # pair + 1 output register, PLUS one clock per CHAINED triple
        # (m >= 1) -- the wrapper's DLY (see rtl/fft_sdf_r23.v)
        from golden import R23ChainGoldenModel
        if cfg.ssr > 1:
            from golden_ssr import SSRGoldenModel
            return SSRGoldenModel(cfg, arch="r23").latency
        return R23ChainGoldenModel(cfg).rtl_lat
    from golden import OrderedFFTModel
    if cfg.ssr > 1:
        from golden_ssr import SSRGoldenModel
        return SSRGoldenModel(cfg).latency
    import dataclasses
    nat_in, nat_out = (("bitreversed", "native") if cfg.is_dit
                       else ("native", "bitreversed"))
    core = SDFGoldenModel_latent(dataclasses.replace(
        cfg, input_order=nat_in, output_order=nat_out), dit=cfg.is_dit)
    lat = core.latency
    if reorder_out(cfg):
        lat += cfg.num_points          # ping-pong reorder depth
    return lat


def SDFGoldenModel_latent(cfg, dit):
    from golden import SDFGoldenModel
    return SDFGoldenModel(cfg, dit=dit)


# ----------------------------------------------------------------------
# fft_params.vh
# ----------------------------------------------------------------------

def engine_cfg(cfg: FFTConfig) -> FFTConfig:
    """The config whose per-stage schedule feeds the RTL generics: the
    M-point lane engine for SSR, the transform itself for R=1."""
    return lane_cfg(cfg) if cfg.ssr > 1 else cfg


def params_header(cfg: FFTConfig) -> str:
    eng = engine_cfg(cfg)
    pack = scaling_pack(eng)
    lines = [
        "// fft_params.vh -- concrete parameters for this FFT build",
        "// (generated by src/export_core.py -- do not edit).",
        "//",
        "// Nothing includes this file automatically; it documents the build",
        "// and can be used by surrounding logic. The generated fft_core.v",
        "// (R=1) bakes the same values into the fft_top instantiation.",
        "",
        "// architecture",
        f"`define FFT_STAGE_MODE      \"{cfg.stage_mode}\"",
        f"`define FFT_NUM_POINTS      {cfg.num_points}",
        f"`define FFT_SSR             {cfg.ssr}",
        f"`define FFT_INVERSE         {1 if cfg.inverse else 0}",
        f"`define FFT_INPUT_ORDER     \"{cfg.input_order}\"",
        f"`define FFT_OUTPUT_ORDER    \"{cfg.output_order}\"",
        f"`define FFT_TOPOLOGY        {1 if cfg.is_dit else 0}      // 0=DIF 1=DIT",
        f"`define FFT_REORDER_OUT     {1 if reorder_out(cfg) else 0}",
        f"`define FFT_NUM_STAGES      {cfg.num_stages}",
        f"`define FFT_PIPE_DEPTH      10     // NLAYERS (golden _SDFStage)",
        "",
        "// widths / quantization",
        f"`define FFT_SAMPLE_WIDTH    {cfg.sample_width}",
        f"`define FFT_SAMPLE_DECIMAL  {cfg.sample_decimal}",
        f"`define FFT_OUTPUT_WIDTH    {cfg.output_width}",
        f"`define FFT_OUTPUT_DECIMAL  {cfg.output_decimal}",
        f"`define FFT_TWIDDLE_WIDTH   {cfg.twiddle_width}",
        f"`define FFT_TWIDDLE_DECIMAL {cfg.twiddle_decimal}",
        f"`define FFT_SCALING_PACK    32'h{pack:08x}  "
            "// 2 bits/stage, stage 0 in LSBs"
            + (" (per-lane engine)" if cfg.ssr > 1 else ""),
        f"`define FFT_INTERN_WIDTH    {intern_width(eng)}",
        "",
        "// scaling schedule (per stage)",
    ]
    for s, sh in enumerate(cfg.shifts):
        lines.append(f"//   stage {s:2d}: shift {sh}")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# generated fft_core.v (R=1): fft_top module with baked parameters
# ----------------------------------------------------------------------

def _replace_many(text, subs):
    for k, v in subs.items():
        text = text.replace(k, v)
    return text.splitlines()


def core_wrapper_r1(cfg: FFTConfig) -> str:
    """Generated replacement for rtl/fft_top.v with every parameter baked.

    Module name stays fft_top so the shipped testbench and any IP-style
    integration see the same interface as the repository wrapper. Only
    valid for ssr == 1 (the SSR top keeps its generics; see params.txt).
    """
    assert cfg.ssr == 1
    ro = reorder_out(cfg)
    iw = intern_width(cfg)
    pack = scaling_pack(cfg)

    def sdf_inst(indent="        "):
        return "\n".join([
            f"{indent}fft_sdf #(",
            f"{indent}    .NUM_POINTS     ({cfg.num_points}),",
            f"{indent}    .SAMPLE_WIDTH   ({cfg.sample_width}),",
            f"{indent}    .SAMPLE_DECIMAL ({cfg.sample_decimal}),",
            f"{indent}    .OUTPUT_WIDTH   ({cfg.output_width}),",
            f"{indent}    .OUTPUT_DECIMAL ({cfg.output_decimal}),",
            f"{indent}    .TWIDDLE_WIDTH  ({cfg.twiddle_width}),",
            f"{indent}    .TWIDDLE_DECIMAL({cfg.twiddle_decimal}),",
            f"{indent}    .SCALING_PACK   (32'h{pack:08x}),",
            f"{indent}    .TOPOLOGY       ({1 if cfg.is_dit else 0}),",
            f"{indent}    .TWIDDLE_FILE   (\"fft_twiddles.mem\"),",
            f"{indent}    .INTERN_WIDTH   ({iw}),",
            f"{indent}    .PIPE_DEPTH     (10),",
            f"{indent}    .TWIDDLE_MEM    (TWIDDLE_MEM)",
            f"{indent}) u_core (",
            f"{indent}    .clk            (clk),",
            f"{indent}    .ce             (ce),",
            f"{indent}    .s_axis_tvalid  (s_axis_tvalid),",
            f"{indent}    .s_axis_tdata_re(s_axis_tdata_re),",
            f"{indent}    .s_axis_tdata_im(s_axis_tdata_im),",
            f"{indent}    .s_axis_tuser   (s_axis_tuser),",
            f"{indent}    .s_axis_tlast   (s_axis_tlast),",
            f"{indent}    .m_axis_tvalid  (core_valid),",
            f"{indent}    .m_axis_tdata_re(core_re),",
            f"{indent}    .m_axis_tdata_im(core_im),",
            f"{indent}    .m_axis_tuser   (core_user),",
            f"{indent}    .m_axis_tlast   (core_last),",
            f"{indent}    .rst            (rst)",
            f"{indent});",
        ])

    if ro:
        body = "\n".join([
            "    wire        core_valid;",
            "    wire signed [INTERN_WIDTH-1:0] core_re, core_im;",
            "    wire        core_user, core_last;",
            "",
            sdf_inst(),
            "",
            "    fft_reorder #(",
            f"        .NUM_POINTS ({cfg.num_points}),",
            f"        .WIDTH      ({cfg.output_width})",
            "    ) u_reorder (",
            "        .clk            (clk),",
            "        .ce             (ce),",
            "        .s_axis_tvalid  (core_valid),",
            "        .s_axis_tdata_re(core_re),",
            "        .s_axis_tdata_im(core_im),",
            "        .s_axis_tuser   (core_user),",
            "        .s_axis_tlast   (core_last),",
            "        .m_axis_tvalid  (m_axis_tvalid),",
            "        .m_axis_tdata_re(m_axis_tdata_re),",
            "        .m_axis_tdata_im(m_axis_tdata_im),",
            "        .m_axis_tuser   (m_axis_tuser),",
            "        .m_axis_tlast   (m_axis_tlast),",
            "        .rst            (rst)",
            "    );",
        ])
    else:
        # direct connection: core outputs ARE the module outputs
        direct = {"core_valid": "m_axis_tvalid",
                  "(core_re)": "(m_axis_tdata_re)",
                  "(core_im)": "(m_axis_tdata_im)",
                  "core_user": "m_axis_tuser",
                  "core_last": "m_axis_tlast"}
        body = "\n".join(line for line in
                         _replace_many(sdf_inst(), direct))

    return "\n".join([
        "// fft_core.v -- GENERATED by src/export_core.py; do not edit.",
        "// Regenerate with:",
        "//   python3 src/export_core.py ... --outdir <this directory>",
        "//",
        "// fft_top module with this build's concrete parameters baked in",
        f"// ({cfg.num_points}-point {'IFFT' if cfg.inverse else 'FFT'}, "
        f"{cfg.input_order}->{cfg.output_order}, "
        f"Q{cfg.sample_decimal} in / Q{cfg.output_decimal} out).",
        "//",
        "// NOTE: this file REPLACES the generic rtl/fft_top.v of the source",
        "// tree -- compile it together with fft_sdf.v/fft_reorder.v INSTEAD",
        "// of the repository wrapper.",
        "//",
        "// The preload pack below feeds fft_sdf's post-warm FSM state; the",
        "// include makes the define visible before fft_sdf.v is parsed",
        "// (keep this file first in the compile order).",
        "`include \"fft_preloads.vh\"",
        "",
        "`default_nettype none",
        "",
        "module fft_top #(",
        f"    parameter integer NUM_POINTS     = {cfg.num_points},",
        f"    parameter integer SAMPLE_WIDTH   = {cfg.sample_width},",
        f"    parameter integer SAMPLE_DECIMAL = {cfg.sample_decimal},",
        f"    parameter integer OUTPUT_WIDTH   = {cfg.output_width},",
        f"    parameter integer OUTPUT_DECIMAL = {cfg.output_decimal},",
        f"    parameter integer TWIDDLE_WIDTH  = {cfg.twiddle_width},",
        f"    parameter integer TWIDDLE_DECIMAL = {cfg.twiddle_decimal},",
        f"    parameter integer SCALING_PACK   = 32'h{pack:08x},",
        f"    parameter integer TOPOLOGY       = {1 if cfg.is_dit else 0},",
        f"    parameter integer REORDER_OUT    = {1 if ro else 0},",
        "    parameter TWIDDLE_FILE           = \"fft_twiddles.mem\",",
        f"    parameter integer INTERN_WIDTH   = {iw},",
        "    parameter integer PIPE_DEPTH     = 10,",
        "    parameter integer TWIDDLE_MEM   = 0        // 0=auto 1=distributed 2=block",
        ")(",
        "    input  wire                        clk,",
        "    input  wire                        ce,",
        "    input  wire                        s_axis_tvalid,",
        f"    input  wire [SAMPLE_WIDTH-1:0]     s_axis_tdata_re,",
        f"    input  wire [SAMPLE_WIDTH-1:0]     s_axis_tdata_im,",
        "    input  wire                        s_axis_tuser,",
        "    input  wire                        s_axis_tlast,",
        "",
        "    output wire                        m_axis_tvalid,",
        "    output wire [OUTPUT_WIDTH-1:0]     m_axis_tdata_re,",
        "    output wire [OUTPUT_WIDTH-1:0]     m_axis_tdata_im,",
        "    output wire                        m_axis_tuser,",
        "    output wire                        m_axis_tlast,",
        "",
        "    input  wire                        rst",
        ");",
        "",
        "    localparam integer INTERN_WIDTH_L = INTERN_WIDTH;",
        body,
        "",
        "endmodule",
        "",
        "`default_nettype wire",
        "",
    ])


# ----------------------------------------------------------------------
# human-readable artifacts
# ----------------------------------------------------------------------

def core_wrapper_r22(cfg: FFTConfig) -> str:
    """Generated fft_core.v for the r22 build: module fft_top with every
    parameter baked in (instantiates fft_sdf_r22). Same interface as the
    r2 wrapper, so tb/tb_fft_sdf.cpp is reusable verbatim.

    No fft_preloads.vh: the r22 leftover preloads are parity-computed
    inside fft_sdf_r22.v and each pair's phase offset is a localparam
    (K_PRELOAD), not generator-supplied state.
    """
    assert cfg.ssr == 1 and cfg.is_r22
    iw = intern_width(cfg)
    pack = scaling_pack(cfg)
    return "\n".join([
        "// fft_core.v -- GENERATED by src/export_core.py; do not edit.",
        "// Regenerate with:",
        "//   python3 src/export_core.py --stage-mode r22 ... --outdir <this dir>",
        "//",
        f"// {cfg.num_points}-point {'IFFT' if cfg.inverse else 'FFT'}, "
        f"{cfg.input_order}->{cfg.output_order}, "
        f"Q{cfg.sample_decimal} in / Q{cfg.output_decimal} out, "
        "radix-2^2 folded (P7).",
        "//",
        "// Contract note: the r22 folding re-pins the product rounding",
        "// points vs plain radix-2 (results differ from an r2 build by",
        "// 1-2 LSB with identical SQNR; see spikes/S5_r22/notes.md).",
        "//",
        "// Compile with: fft_sdf_r22.v fft_stage_r22.v fft_sdf.v",
        "// (fft_sdf.v provides only the plain fft_stage used for the",
        "//  odd-stage-count leftover; no FFTGEN_PRELOADS define needed).",
        "",
        "`default_nettype none",
        "",
        "module fft_top #(",
        f"    parameter integer NUM_POINTS     = {cfg.num_points},",
        f"    parameter integer SAMPLE_WIDTH   = {cfg.sample_width},",
        f"    parameter integer SAMPLE_DECIMAL = {cfg.sample_decimal},",
        f"    parameter integer OUTPUT_WIDTH   = {cfg.output_width},",
        f"    parameter integer OUTPUT_DECIMAL = {cfg.output_decimal},",
        f"    parameter integer TWIDDLE_WIDTH  = {cfg.twiddle_width},",
        f"    parameter integer TWIDDLE_DECIMAL = {cfg.twiddle_decimal},",
        f"    parameter integer SCALING_PACK   = 32'h{pack:08x},",
        f"    parameter integer INVERSE        = {1 if cfg.inverse else 0},",
        "    parameter integer TOPOLOGY       = 0,",
        "    parameter integer REORDER_OUT    = 0,",
        "    parameter TWIDDLE_FILE           = \"fft_twiddles_r22.mem\",",
        f"    parameter integer INTERN_WIDTH   = {iw},",
        "    parameter integer PIPE_DEPTH     = 10,   // unused (compat)",
        "    parameter integer TWIDDLE_MEM    = 0     // 0=auto 1=distributed 2=block",
        ")(",
        "    input  wire                        clk,",
        "    input  wire                        ce,",
        "    input  wire                        s_axis_tvalid,",
        f"    input  wire [SAMPLE_WIDTH-1:0]     s_axis_tdata_re,",
        f"    input  wire [SAMPLE_WIDTH-1:0]     s_axis_tdata_im,",
        "    input  wire                        s_axis_tuser,",
        "    input  wire                        s_axis_tlast,",
        "",
        "    output wire                        m_axis_tvalid,",
        "    output wire [OUTPUT_WIDTH-1:0]     m_axis_tdata_re,",
        "    output wire [OUTPUT_WIDTH-1:0]     m_axis_tdata_im,",
        "    output wire                        m_axis_tuser,",
        "    output wire                        m_axis_tlast,",
        "",
        "    input  wire                        rst",
        ");",
        "",
        "    fft_sdf_r22 #(",
        "        .NUM_POINTS     (NUM_POINTS),",
        "        .SAMPLE_WIDTH   (SAMPLE_WIDTH),",
        "        .SAMPLE_DECIMAL (SAMPLE_DECIMAL),",
        "        .OUTPUT_WIDTH   (OUTPUT_WIDTH),",
        "        .OUTPUT_DECIMAL (OUTPUT_DECIMAL),",
        "        .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),",
        "        .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),",
        "        .SCALING_PACK   (SCALING_PACK),",
        "        .INVERSE        (INVERSE),",
        "        .TOPOLOGY       (TOPOLOGY),",
        "        .TWIDDLE_FILE   (TWIDDLE_FILE),",
        "        .INTERN_WIDTH   (INTERN_WIDTH),",
        "        .PIPE_DEPTH     (PIPE_DEPTH),",
        "        .TWIDDLE_MEM    (TWIDDLE_MEM)",
        "    ) u_core (",
        "        .clk            (clk),",
        "        .ce             (ce),",
        "        .s_axis_tvalid  (s_axis_tvalid),",
        "        .s_axis_tdata_re(s_axis_tdata_re),",
        "        .s_axis_tdata_im(s_axis_tdata_im),",
        "        .s_axis_tuser   (s_axis_tuser),",
        "        .s_axis_tlast   (s_axis_tlast),",
        "        .m_axis_tvalid  (m_axis_tvalid),",
        "        .m_axis_tdata_re(m_axis_tdata_re),",
        "        .m_axis_tdata_im(m_axis_tdata_im),",
        "        .m_axis_tuser   (m_axis_tuser),",
        "        .m_axis_tlast   (m_axis_tlast),",
        "        .rst            (rst)",
        "    );",
        "",
        "endmodule",
        "",
        "`default_nettype wire",
        "",
    ])



def core_wrapper_r23(cfg: FFTConfig) -> str:
    """Generated fft_core.v for the r23 build: module fft_top with every
    parameter baked in (instantiates fft_sdf_r23). Same interface as the
    r2/r22 wrappers, so tb/tb_fft_sdf.cpp is reusable verbatim.

    The four ROM files (3 triple slices + the concatenated r22 leftover)
    are written alongside; the TWIDDLE_FILE_* defaults point at them.
    """
    assert cfg.ssr == 1 and cfg.stage_mode == "r23"
    iw = intern_width(cfg)
    pack = scaling_pack(cfg)
    return "\n".join([
        "// fft_core.v -- GENERATED by src/export_core.py; do not edit.",
        "// Regenerate with:",
        "//   python3 src/export_core.py --stage-mode r23 ... --outdir <this dir>",
        "//",
        f"// {cfg.num_points}-point {'IFFT' if cfg.inverse else 'FFT'}, "
        f"{cfg.input_order}->{cfg.output_order}, "
        f"Q{cfg.sample_decimal} in / Q{cfg.output_decimal} out, "
        "radix-2^3 folded (S7).",
        "//",
        "// Compile with: fft_sdf_r23.v fft_stage_r23.v fft_stage_r22.v",
        "// (+ the four fft_tw_*.mem ROM files written alongside).",
        "//",
        "// Contract note: the r23 folding re-pins the product rounding",
        "// points vs plain radix-2 (same class of few-LSB delta as r22;",
        "// see spikes/S7_r23/notes.md). The core emits bit-reversed bins",
        "// (DIF); pair with fft_reorder.v for natural order.",
        "",
        "`default_nettype none",
        "",
        "module fft_top #(",
        f"    parameter integer NUM_POINTS     = {cfg.num_points},",
        f"    parameter integer SAMPLE_WIDTH   = {cfg.sample_width},",
        f"    parameter integer SAMPLE_DECIMAL = {cfg.sample_decimal},",
        f"    parameter integer OUTPUT_WIDTH   = {cfg.output_width},",
        f"    parameter integer OUTPUT_DECIMAL = {cfg.output_decimal},",
        f"    parameter integer TWIDDLE_WIDTH  = {cfg.twiddle_width},",
        f"    parameter integer TWIDDLE_DECIMAL = {cfg.twiddle_decimal},",
        f"    parameter integer SCALING_PACK   = 32'h{pack:08x},",
        f"    parameter integer INVERSE        = {1 if cfg.inverse else 0},",
        "    parameter TWIDDLE_FILE_T0        = \"fft_tw_r23_t0.mem\",",
        "    parameter TWIDDLE_FILE_T1        = \"fft_tw_r23_t1.mem\",",
        "    parameter TWIDDLE_FILE_T2        = \"fft_tw_r23_t2.mem\",",
        "    parameter TWIDDLE_FILE_L         = \"fft_tw_r22_l.mem\",",
        f"    parameter integer INTERN_WIDTH   = {iw},",
        "    parameter integer USE_URAM       = 0",
        ")(",
        "    input  wire                        clk,",
        "    input  wire                        ce,",
        "    input  wire                        s_axis_tvalid,",
        f"    input  wire [SAMPLE_WIDTH-1:0]     s_axis_tdata_re,",
        f"    input  wire [SAMPLE_WIDTH-1:0]     s_axis_tdata_im,",
        "    input  wire                        s_axis_tuser,",
        "    input  wire                        s_axis_tlast,",
        "",
        "    output wire                        m_axis_tvalid,",
        "    output wire [OUTPUT_WIDTH-1:0]     m_axis_tdata_re,",
        "    output wire [OUTPUT_WIDTH-1:0]     m_axis_tdata_im,",
        "    output wire                        m_axis_tuser,",
        "    output wire                        m_axis_tlast,",
        "",
        "    input  wire                        rst",
        ");",
        "",
        "    fft_sdf_r23 #(",
        "        .NUM_POINTS     (NUM_POINTS),",
        "        .SAMPLE_WIDTH   (SAMPLE_WIDTH),",
        "        .SAMPLE_DECIMAL (SAMPLE_DECIMAL),",
        "        .OUTPUT_WIDTH   (OUTPUT_WIDTH),",
        "        .OUTPUT_DECIMAL (OUTPUT_DECIMAL),",
        "        .TWIDDLE_WIDTH  (TWIDDLE_WIDTH),",
        "        .TWIDDLE_DECIMAL(TWIDDLE_DECIMAL),",
        "        .SCALING_PACK   (SCALING_PACK),",
        "        .INVERSE        (INVERSE),",
        "        .TWIDDLE_FILE_T0(TWIDDLE_FILE_T0),",
        "        .TWIDDLE_FILE_T1(TWIDDLE_FILE_T1),",
        "        .TWIDDLE_FILE_T2(TWIDDLE_FILE_T2),",
        "        .TWIDDLE_FILE_L (TWIDDLE_FILE_L),",
        "        .INTERN_WIDTH   (INTERN_WIDTH),",
        "        .USE_URAM       (USE_URAM)",
        "    ) u_core (",
        "        .clk            (clk),",
        "        .ce             (ce),",
        "        .s_axis_tvalid  (s_axis_tvalid),",
        "        .s_axis_tdata_re(s_axis_tdata_re),",
        "        .s_axis_tdata_im(s_axis_tdata_im),",
        "        .s_axis_tuser   (s_axis_tuser),",
        "        .s_axis_tlast   (s_axis_tlast),",
        "        .m_axis_tvalid  (m_axis_tvalid),",
        "        .m_axis_tdata_re(m_axis_tdata_re),",
        "        .m_axis_tdata_im(m_axis_tdata_im),",
        "        .m_axis_tuser   (m_axis_tuser),",
        "        .m_axis_tlast   (m_axis_tlast),",
        "        .rst            (rst)",
        "    );",
        "",
        "endmodule",
        "",
        "`default_nettype wire",
        "",
    ])


# ----------------------------------------------------------------------
# human-readable artifacts (unchanged helpers; r22 branches inline)
# ----------------------------------------------------------------------

def twiddle_map_txt(cfg: FFTConfig) -> str:
    """Twiddle quantization contract: layout, rule, and every ROM word."""
    from twiddles import canonical_twiddles
    N = cfg.num_points
    td = cfg.twiddle_decimal
    tw = canonical_twiddles(N, cfg.twiddle_width, td, cfg.inverse)
    out = []
    out.append("twiddle_map.txt -- twiddle ROM contract (generated)")
    out.append("=" * 60)
    out.append(f"N={N}  inverse={int(cfg.inverse)}  ssr={cfg.ssr}  "
               f"width={cfg.twiddle_width}  Q{td}")
    out.append("")
    out.append("quantization: sat(round_half_up(2^%d * cos/sin)) clamped to" % td)
    out.append("  signed %d-bit range [-2^%d, 2^%d-1]; the identity W=1 maps"
               % (cfg.twiddle_width, cfg.twiddle_width - 1,
                  cfg.twiddle_width - 1))
    out.append("  to 131071 (one LSB below full scale) consistently in RTL")
    out.append("  ROM and golden model.")
    out.append("")
    out.append("ROM word format: hex, {re, im} packed MSB:LSB, %d bits/word."
               % (2 * cfg.twiddle_width))
    out.append("")
    if cfg.is_r22 and cfg.ssr == 1:
        out.append("r22 pair layout (fft_sdf_r22.v TWIDDLE_FILE): pair m")
        out.append("  occupies rows [BASE_m, BASE_m + 3*D_m) with the three")
        out.append("  slices [T[g*4^m]], [T[2g*4^m]], [T[3g*4^m]],")
        out.append("  g in [0, D_m), D_m = N/4^{m+1}, BASE_m = sum(3*D_t,t<m).")
        out.append("  Odd stage count appends one W^0 word for the leftover.")
        out.append("  NOTE: fft_stage_r22.v declares the ROM as NPTS words;")
        out.append("  rows past the file end are unused (read as X in sim,")
        out.append("  never addressed).")
        out.append("")
        out.append("idx  pair  slice  g   k (exp of W_N^k)   re_int   im_int")
        base = 0
        m = 0
        while 2 * m + 1 < cfg.num_stages:
            D = N >> (2 * m + 2)
            stride = 4 ** m
            for which in (1, 2, 3):
                for g in range(D):
                    k = (which * g * stride) % N
                    re, im = tw[k]
                    out.append(f"{base:5d}  {m:3d}  {which:4d} {g:4d}"
                               f"  {k:5d}  {re:8d} {im:8d}")
                    base += 1
            m += 1
        if cfg.num_stages % 2:
            re, im = tw[0]
            out.append(f"{base:5d}  lo    W^0   0      0  {re:8d} {im:8d}")
        return "\n".join(out) + "\n"
    if cfg.ssr == 1:
        dit = cfg.is_dit
        out.append("stage layout (fft_sdf.v TWIDDLE_FILE, one N-word table):")
        base = 0
        for s in range(cfg.num_stages):
            if dit:
                D = 1 << s
                idxs = [(j << (cfg.num_stages - s - 1)) % N for j in range(D)]
                stride = N >> (s + 1)
            else:
                D = N >> (s + 1)
                idxs = [(i << s) % N for i in range(D)]
                stride = 1 << s
            out.append(f"  stage {s}: rows [{base}..{base + D - 1}], "
                       f"D={D}, k = i*{stride}" +
                       (" (DIT)" if dit else " (DIF)"))
            base += D
        out.append("")
        out.append("entries (idx = ROM address, k = exp of W_N^k):")
        out.append("idx  k      re_float      im_float      re_int   im_int")
        base = 0
        for s in range(cfg.num_stages):
            if dit:
                D = 1 << s
                idxs = [(j << (cfg.num_stages - s - 1)) % N for j in range(D)]
            else:
                D = N >> (s + 1)
                idxs = [(i << s) % N for i in range(D)]
            out.append(f"-- stage {s} --")
            for j, k in enumerate(idxs):
                re, im = tw[k]
                import math
                ang = -2 * math.pi * k / N
                if cfg.inverse:
                    ang = -ang
                out.append(f"{base + j:4d}  {k:5d}  {math.cos(ang):+.6f}  "
                           f"{math.sin(ang):+.6f}  {re:8d} {im:8d}")
            base += D
    else:
        R, M = cfg.ssr, cfg.num_points // cfg.ssr
        out.append("crossbar WN table (fft_wn.mem): row r holds W_N^{r*p},")
        out.append(f"  p in [0,{M}); {R*M} words total.")
        out.append("")
        out.append("word  r  p      k      re_int   im_int")
        w = 0
        for r in range(R):
            for p in range(M):
                k = (r * p) % N
                re, im = tw[k]
                out.append(f"{w:5d}  {r}  {p:3d}  {k:5d}  {re:8d} {im:8d}")
                w += 1
    return "\n".join(out) + "\n"


def params_txt(cfg: FFTConfig, extra: dict) -> str:
    """Human-readable record of the chosen parameters."""
    iw = intern_width(cfg)
    mode = cfg.stage_mode
    lines = [
        f"core                = " + (
            "fft_ssr_r22_inv" if (cfg.input_order == "bitreversed") else
            "fft_ssr_r22" if (cfg.ssr > 1 and mode == "r22") else
            "fft_ssr_r23" if (cfg.ssr > 1 and mode == "r23") else
            "fft_ssr" if cfg.ssr > 1 else
            "fft_sdf_r23" if mode == "r23" else
            "fft_sdf_r22" if mode == "r22" else "fft_sdf"),
        f"stage_mode          = {mode}",
        f"tolerance           = {0 if cfg.input_order == 'bitreversed' else 0 if cfg.ssr == 1 else cfg.ssr // 2 + 1}",
        f"num_points          = {cfg.num_points}",
        f"ssr                 = {cfg.ssr}",
        f"inverse             = {int(cfg.inverse)}",
        f"input_order         = {cfg.input_order}",
        f"output_order        = {cfg.output_order}",
        f"topology            = "
        + ("transpose (DIF lanes)" if cfg.input_order == 'bitreversed'
           else 'DIT' if cfg.is_dit else 'DIF'),
        f"reorder_out         = "
        + ("1 (lane output; +2 lane input reorders)"
           if cfg.input_order == 'bitreversed'
           else str(int(reorder_out(cfg)))),
        f"num_stages          = {cfg.num_stages}",
        (f"stage_pairs         = {cfg.num_stages // 2}"
         + (" + 1 leftover (D=1, trivial)" if cfg.num_stages % 2 else "")
         if mode == "r22" else "pipe_depth          = 10                    (NLAYERS)"),
        f"sample_width        = {cfg.sample_width}",
        f"sample_decimal      = {cfg.sample_decimal}",
        f"output_width        = {cfg.output_width}",
        f"output_decimal      = {cfg.output_decimal}",
        f"twiddle_width       = {cfg.twiddle_width}",
        f"twiddle_decimal     = {cfg.twiddle_decimal}",
        f"intern_width        = {iw}",
        f"scaling             = {cfg.scaling if cfg.scaling != 'auto' else 'auto'}",
        f"scaling_shifts      = {list(cfg.shifts)}",
        f"scaling_guaranteed  = {int(cfg.scaling_guaranteed)}",
        f"scaling_pack        = 32'h{scaling_pack(cfg):08x}",
        f"rounding            = round-half-up at scaling shifts; one fused",
        "                      round at twiddle_decimal+sigma on products",
        f"latency_cycles      = {extra['latency']}",
        f"throughput          = 1 frame / {extra['frames_per_clock_den']}"
        " clocks (ce high)",
        f"dsp_estimate        = {extra['dsp']}",
        f"preload_bits        = {extra['preload_bits']}",
        "",
        "memory styles (doc/mem_cutoffs.md cutoffs:",
        "  <=1024 bits distributed; <262144 block; else ultra):",
    ]
    if mode == "r22":
        lines[-2:] = [
            "memory styles (r22 v1 policy, rtl/fft_stage_r22.v):",
            "  delay lines + product FIFO: distributed LUTRAM; twiddle ROM:",
            "  block RAM (replicated per pair; rows beyond the ROM file are",
            "  never addressed). See datasheet LUTRAM column.",
        ]
    if mode == "r23":
        lines[-2:] = [
            "memory styles (r23, rtl/fft_stage_r23.v + fft_stage_r22.v):",
            "  triple rings + product FIFOs: block RAM (auto at large G);",
            "  twiddle ROMs: block RAM (USE_URAM=1 moves the big rings to",
            "  URAM288 on 500MHz-capable URAM parts -- doc/uram_study.md).",
        ]
    if cfg.ssr == 1 and mode != "r22":
        for s in range(cfg.num_stages):
            D = (1 << s) if cfg.is_dit else (cfg.num_points >> (s + 1))
            bits = 2 * D * cfg.sample_width
            style = mem_style_bits(bits)
            lines.append(f"  stage {s:2d}: delay/pfifo {2 * D:5d} x "
                         f"{cfg.sample_width:2d}b = {bits:7d} bits -> {style}")
        rbits = 2 * cfg.num_points * cfg.output_width
        if reorder_out(cfg):
            lines.append(f"  reorder   : {2 * cfg.num_points:5d} x "
                         f"{cfg.output_width:2d}b = {rbits:7d} bits -> "
                         f"{mem_style_bits(rbits)}")
    if cfg.ssr > 1:
        lc = lane_cfg(cfg)
        for s in range(lc.num_stages):
            D = lc.num_points >> (s + 1)
            bits = 2 * D * cfg.sample_width
            lines.append(f"  lane st{s:2d}: {2 * D:5d} x {cfg.sample_width:2d}b"
                         f" = {bits:7d} bits -> {mem_style_bits(bits)}"
                         f"  (x{cfg.ssr} lanes)")
        rbits = 2 * lc.num_points * cfg.output_width
        if cfg.input_order == "bitreversed":
            # P8 4b corner-inverse: the transpose adds the per-lane INPUT
            # reorders (bitrev_M arrival -> native) in front of each lane;
            # each lane also keeps its own output reorder (below)
            rbits_in = 2 * lc.num_points * cfg.sample_width
            lines.append(f"  lane in-reorder: {2 * lc.num_points:4d} x "
                         f"{cfg.sample_width:2d}b = {rbits_in:7d} bits -> "
                         f"{mem_style_bits(rbits_in)}  (x{cfg.ssr} lanes)")
        if reorder_out(cfg):
            lines.append(f"  lane reorder: {2 * lc.num_points:5d} x "
                         f"{cfg.output_width:2d}b = {rbits:7d} bits -> "
                         f"{mem_style_bits(rbits)}  (x{cfg.ssr} lanes)")
        else:
            # P8 corner order: the DIF lanes already emit bit-reversed p,
            # so no reorder buffer is instantiated at all.
            lines.append(f"  lane reorder: none ({rbits // 8} x 8 bits/lane "
                         f"saved, x{cfg.ssr} lanes) -- bitrev output order")
    if mode == "r22" and cfg.ssr == 1:
        m = 0
        while 2 * m + 1 < cfg.num_stages:
            D = cfg.num_points >> (2 * m + 2)
            bits = (2 + 1 + 1 + 1 + 2) * D * 2 * iw   # ram/sram/dram/dline/pfifo
            lines.append(f"  pair {m:2d}: 8D x {iw}b complex = {bits:7d} bits"
                         f" -> distributed (v1)")
            m += 1
        rom_bits = (2 * cfg.num_stages // 2 + 1) * 2 * cfg.num_points * cfg.twiddle_width
        lines.append(f"  tw ROM  : {cfg.num_points:5d} x "
                     f"{2 * cfg.twiddle_width}b x {cfg.num_stages // 2} "
                     f"= ~{2 * cfg.num_points * cfg.twiddle_width} bits/copy"
                     f" -> block (v1)")
    return "\n".join(lines) + "\n"


def readme_txt(cfg: FFTConfig, args) -> str:
    ssr = cfg.ssr > 1
    r22 = cfg.is_r22
    r23 = cfg.stage_mode == "r23"
    corner_inv = cfg.input_order == "bitreversed"
    top = ("fft_ssr_r22_inv" if corner_inv else
           "fft_ssr_r22" if (ssr and r22) else
           "fft_ssr_r23" if (ssr and r23) else "fft_ssr" if ssr
           else "fft_top")
    files = (([] if ssr else ["fft_core.v"]) + core_files(cfg))
    srcs = " ".join(files)
    tbname = ("tb_fft_ssr_r22_inv.cpp" if corner_inv else
              "tb_fft_ssr_r22.cpp" if (ssr and r22) else
              "tb_fft_ssr_r23.cpp" if (ssr and r23) else
              "tb_fft_ssr.cpp" if ssr else "tb_fft_sdf.cpp")
    binary = "V" + top

    contents = []
    if not ssr:
        contents += [
            "  fft_core.v        generated top (replaces the repo's",
            "                    generic fft_top.v -- parameters baked in)",
        ]
    if r23:
        contents += [
            "  fft_sdf_r23.v     radix-2^3 SDF stage chain (S7)",
            "  fft_stage_r23.v   the R2^3 kernel stage",
            "  fft_stage_r22.v   the R2^2 leftover pair stage",
        ]
    elif r22:
        contents += [
            "  fft_sdf_r22.v     radix-2^2 SDF stage chain (P7)",
            "  fft_stage_r22.v   the R2^2 folded pair stage",
            "  fft_sdf.v         plain fft_stage module (odd-n leftover)",
        ]
    else:
        contents += [
            "  fft_sdf.v         radix-2 SDF stage chain (10-layer pipeline)",
            "  fft_reorder.v     ping-pong bit-reversal reorder",
        ]
    if ssr:
        contents += [
            ("  fft_ssr_r22_inv.v SSR corner-order IFFT top (transpose route)"
             if corner_inv else
             "  fft_ssr_r23.v     SSR top: r23 lanes + crossbar" if r23 else
             "  fft_ssr_r22.v     SSR top: r22 lanes + crossbar" if r22 else
             "  fft_ssr.v         SSR top: R lane engines + crossbar"),
            ("  fft_top_r22.v     r22 lane wrapper (+ fft_reorder)" if r22 else
             "  fft_top.v         generic lane wrapper (+ fft_reorder)"
             if not r23 else
             "  (r23 lanes are whole fft_sdf_r23 cores -- no lane wrapper)"),
            ("  (no fft_cross: the R-point step runs FIRST in the transpose)"
             if corner_inv else
             "  fft_cross.v       cross-lane combine (shared r2/r22/r23)"),
        ]
    if r23:
        contents += [
            "  fft_tw_r23_t{0,1,2}.mem  r23 triple ROMs ($readmemh)",
            "  fft_tw_r22_l.mem         concatenated leftover-pair ROM",
        ]
        if ssr:
            contents += ["  fft_wn.mem               crossbar pre-twiddle ROM"]
    elif r22:
        contents += [
            ("  fft_twiddles_r22.mem  R2^2 pair-sliced twiddle ROM"
             if not ssr else
             "  fft_twiddles_r22_lane.mem  R2^2 lane ROM ($readmemh)"),
        ]
    else:
        contents += ["  fft_twiddles.mem  twiddle ROM ($readmemh)"]
    if ssr and not r23:
        contents += [("  fft_w1_inv.mem    wrapper twiddle ROM W_N^{-p} ($readmemh)"
                      if corner_inv else
                      "  fft_wn.mem        crossbar pre-twiddle ROM")]
    if not r22:
        contents += [
            "  fft_preloads.vh   post-warm FSM preload pack (74 bits/stage)",
        ]
    contents += [
        "  fft_params.vh     concrete parameters as `define macros",
        "  params.txt        human-readable parameter record",
        "  twiddle_map.txt   twiddle layout + quantization contract",
        "  tb/               Verilator testbench (C++)",
        "  stimulus.txt      input vectors (hex re im user last)",
        "  expected.txt      golden-model expected output",
        "  compare.py        VERIFY: python3 compare.py (expected vs actual)",
        "  vivado/synth.tcl  OOC synthesis script (IMP=1 adds P&R)",
        "  vivado/timing.xdc clock constraint",
    ]

    gargs = None
    if ssr:
        gargs = ("    -GNUM_POINTS=%d -GSSR=%d \\\n"
                 "    -GSCALING_PACK=32'h%08x -GINTERN_WIDTH=%d \\\n"
                 "    -GINVERSE=%d"
                 % (cfg.num_points, cfg.ssr, scaling_pack(engine_cfg(cfg)),
                    intern_width(lane_cfg(cfg)), int(cfg.inverse)))
        if cfg.stage_mode != "r23":
            gargs += ' -GPIPE_DEPTH=10'
        if r22 and not corner_inv:
            # P8: REORDER_OUT selects the emission order (1 = native ->
            # native, 0 = native -> bitreversed). It MUST be passed: the
            # RTL default is 1, so an exported corner-order tree would
            # otherwise simulate the wrong order against its own
            # expected.txt (caught by tests/test_export.py).
            gargs += " \\\n    -GREORDER_OUT=%d" % (1 if reorder_out(cfg) else 0)
        if r23:
            # the r23 SSR top's ROM files default to the written names
            gargs += (" \\\n    -GWN_FILE=\"fft_wn.mem\" \\\n"
                      "    -GLANE_TW_T0=\"fft_tw_r23_t0.mem\" \\\n"
                      "    -GLANE_TW_T1=\"fft_tw_r23_t1.mem\" \\\n"
                      "    -GLANE_TW_T2=\"fft_tw_r23_t2.mem\" \\\n"
                      "    -GLANE_TW_L=\"fft_tw_r22_l.mem\"")
        # fft_ssr_r22_inv defaults W1_FILE/LANE_TW_FILE to the written names

    sim = [
        "  verilator --cc --exe --build -j 4 --top-module %s \\" % top,
        ("    -Wno-fatal \\" if (r22 or r23) else
         "    -Wno-fatal +define+FFTGEN_PRELOADS +incdir+. \\"),
        '    -CFLAGS "-DTB_SAMPLE_WIDTH=%d -DTB_OUTPUT_WIDTH=%d%s" \\'
        % (cfg.sample_width, cfg.output_width,
           (" -DTB_SSR=%d" % cfg.ssr) if ssr else ""),
    ]
    if gargs:
        sim.append(gargs + " \\")
    sim += [
        "    %s tb/%s" % (srcs, tbname),
        "    ./obj_dir/%s      # writes actual.txt" % binary,
        "",
        "Verification (REQUIRED -- the simulation above only DUMPS actual.txt;",
        "it prints 'ok: N samples' for any run that completes, correct or not):",
        "",
        "    python3 compare.py      # expected.txt vs actual.txt, exit 0 = pass",
    ]

    if corner_inv:
        note = ("compare.py checks BIT-EXACT values (tolerance 0: the corner\n"
                "IFFT has ONE quantization point, the wrapper's a1 quantize,\n"
                "mirrored exactly by the RTL) plus tuser/tlast positionally\n"
                "and exactly.")
    elif r23 and not ssr:
        note = ("compare.py checks BIT-EXACT values against the r23\n"
                "re-pinned contract (spikes/S7_r23/notes.md): few-LSB vs an\n"
                "r2/r22 build, identical SQNR. tuser/tlast are positional.")
    elif r23:
        note = ("compare.py checks SSR values with tolerance R/2+1 LSB after\n"
                "word-offset alignment (r23 lanes re-pin the rounding\n"
                "contract; measured worst delta 1 LSB), and tuser/tlast\n"
                "positionally and exactly.")
    elif r22 and not ssr:
        note = ("compare.py checks BIT-EXACT values against the r22\n"
                "re-pinned contract (spikes/S5_r22/notes.md): 1-2 LSB vs an\n"
                "r2 build, identical SQNR. tuser/tlast are positional.")
    elif r22:
        note = ("compare.py checks SSR values with tolerance R/2+1 LSB after\n"
                "word-offset alignment (r22 lanes re-pin the rounding\n"
                "contract: 1-2 LSB vs r2, identical SQNR), and tuser/tlast\n"
                "positionally and exactly.")
    elif ssr:
        note = ("compare.py checks bit-exact for R=1; SSR compares\n"
                "with tolerance R/2+1 LSB after word-offset alignment\n"
                "(lane-0 identity twiddle differs from the golden\n"
                "131071-multiply by <1 LSB). Markers are positional.")
    else:
        note = "compare.py checks expected.txt vs actual.txt bit-exactly."

    out = [
        "fftgen exported core -- README (generated)",
        "=" * 50,
        "",
        "Configuration: see params.txt; machine-readable: fft_params.vh.",
        "Numerical contract per ROM word: see twiddle_map.txt.",
        "",
        "Contents:",
    ] + contents + ["", "Simulation (Verilator), from THIS directory:", ""] \
        + sim + ["", note, "", "Synthesis:", "",
                 "  cd vivado && <vivado> -mode batch -source synth.tcl \\",
                 "      -tclargs %s %s" % (args.part, args.clk_mhz),
                 "  Set IMP=1 in synth.tcl (or pass as 3rd arg) for P&R.",
                 ""]
    return "\n".join(out)


def synth_tcl(cfg: FFTConfig, part: str, clk_mhz: float) -> str:
    ssr = cfg.ssr > 1
    r22 = cfg.is_r22
    r23 = cfg.stage_mode == "r23"
    corner_inv = cfg.input_order == "bitreversed"
    top = ("fft_ssr_r22_inv" if corner_inv else
           "fft_ssr_r22" if (ssr and r22) else
           "fft_ssr_r23" if (ssr and r23) else "fft_ssr" if ssr
           else "fft_top")
    # fft_core.v (generated wrapper) leads so its fft_preloads.vh include
    # is parsed before fft_sdf.v regardless of tool define semantics
    files = (([] if ssr else ["fft_core.v"]) + core_files(cfg))
    srcs = " \\\n".join(f"    [file join $rtl_dir {f}]" for f in files)
    mems = (["fft_tw_r23_t0.mem", "fft_tw_r23_t1.mem",
             "fft_tw_r23_t2.mem", "fft_tw_r22_l.mem"]
            if (r23 and not ssr) else
            ["fft_tw_r23_t0.mem", "fft_tw_r23_t1.mem", "fft_tw_r23_t2.mem",
             "fft_tw_r22_l.mem", "fft_wn.mem"] if r23 else
            ["fft_twiddles_r22.mem"] if (r22 and not ssr) else
            ["fft_twiddles_r22_lane.mem", "fft_wn.mem"] if (r22 and ssr and
                                                            not corner_inv)
            else ["fft_twiddles_r22_lane.mem", "fft_w1_inv.mem"]
            if (r22 and corner_inv)
            else ["fft_twiddles.mem"] + (
                ["fft_twiddles_lane.mem", "fft_wn.mem"] if ssr else []))
    mem_copy = "\n".join(
        f'file copy -force [file join $rtl_dir "{m}"] .' for m in mems)
    preload_define = ("" if (r22 or r23) else
        "set_property verilog_define {FFTGEN_PRELOADS=1} [current_fileset]\n")
    generics = ""
    if corner_inv:
        generics = (
            "synth_design -top fft_ssr_r22_inv -generic NUM_POINTS=%d \\\n"
            "    -generic SSR=%d -generic SCALING_PACK=32'h%08x \\\n"
            "    -generic INTERN_WIDTH=%d -generic INVERSE=%d \\\n"
            "    -generic PIPE_DEPTH=10 \\\n"
            "    -generic W1_FILE=\"fft_w1_inv.mem\" \\\n"
            "    -generic LANE_TW_FILE=\"fft_twiddles_r22_lane.mem\""
            % (cfg.num_points, cfg.ssr, scaling_pack(engine_cfg(cfg)),
               intern_width(engine_cfg(cfg)), int(cfg.inverse)))
    elif ssr and r22:
        # REORDER_OUT must be synthesized exactly as the simulation builds it:
        # it selects the emission order (and whether the lane reorder buffers
        # exist at all). Omitting it silently synthesizes a native-order core
        # for a bitrev-order export -- a netlist that cannot match the
        # shipped expected.txt. (P8)
        generics = (
            "synth_design -top fft_ssr_r22 -generic NUM_POINTS=%d \\\n"
            "    -generic SSR=%d -generic SCALING_PACK=32'h%08x \\\n"
            "    -generic INTERN_WIDTH=%d -generic INVERSE=%d \\\n"
            "    -generic PIPE_DEPTH=10 -generic REORDER_OUT=%d \\\n"
            "    -generic WN_FILE=\"fft_wn.mem\" \\\n"
            "    -generic LANE_TW_FILE=\"fft_twiddles_r22_lane.mem\""
            % (cfg.num_points, cfg.ssr, scaling_pack(engine_cfg(cfg)),
               intern_width(engine_cfg(cfg)), int(cfg.inverse),
               1 if reorder_out(cfg) else 0))
    elif ssr and r23:
        generics = (
            "synth_design -top fft_ssr_r23 -generic NUM_POINTS=%d \\\n"
            "    -generic SSR=%d -generic SCALING_PACK=32'h%08x \\\n"
            "    -generic INTERN_WIDTH=%d -generic INVERSE=%d \\\n"
            "    -generic WN_FILE=\"fft_wn.mem\" \\\n"
            "    -generic LANE_TW_T0=\"fft_tw_r23_t0.mem\" \\\n"
            "    -generic LANE_TW_T1=\"fft_tw_r23_t1.mem\" \\\n"
            "    -generic LANE_TW_T2=\"fft_tw_r23_t2.mem\" \\\n"
            "    -generic LANE_TW_L=\"fft_tw_r22_l.mem\""
            % (cfg.num_points, cfg.ssr, scaling_pack(engine_cfg(cfg)),
               intern_width(engine_cfg(cfg)), int(cfg.inverse)))
    elif ssr:
        generics = (
            "synth_design -top fft_ssr -generic NUM_POINTS=%d \\\n"
            "    -generic SSR=%d -generic SCALING_PACK=32'h%08x \\\n"
            "    -generic INTERN_WIDTH=%d -generic INVERSE=%d \\\n"
            "    -generic PIPE_DEPTH=10 \\\n"
            "    -generic WN_FILE=\"fft_wn.mem\" \\\n"
            "    -generic TWIDDLE_FILE=\"fft_twiddles_lane.mem\""
            % (cfg.num_points, cfg.ssr, scaling_pack(engine_cfg(cfg)),
               intern_width(engine_cfg(cfg)), int(cfg.inverse)))
    else:
        generics = "synth_design -top fft_top"
    return """\
# OOC synthesis (and optional implementation) for the exported FFT core.
# usage: vivado -mode batch -source synth.tcl -tclargs <part> <clk_mhz> [IMP]
# Run from the vivado/ subdirectory of the export.
set part   [lindex $argv 0]
set clk_mhz [lindex $argv 1]
set IMP    [expr {[llength $argv] > 2 ? [lindex $argv 2] : 0}]
set rtl_dir [file normalize [file join [file dirname [info script]] ..]]

set period_ns [expr {1000.0 / $clk_mhz}]

# $readmemh resolves relative to the run directory: bring the ROMs here.
""" + mem_copy + """

create_project -in_memory -part $part
add_files -fileset sources_1 [list \\
""" + srcs + """
]
set_property top """ + top + """ [current_fileset]
""" + preload_define + """set_property include_dirs [list $rtl_dir ..] [current_fileset]

""" + generics + """
create_clock -period $period_ns -name clk [get_ports clk]

report_utilization -file utilization.rpt
report_timing_summary -delay_type max -max_paths 10 -file timing.rpt

if {$IMP} {
    opt_design
    place_design
    phys_opt_design
    route_design
    report_utilization -file utilization_impl.rpt
    report_timing_summary -delay_type max -max_paths 10 -file timing_impl.rpt
}
"""


def timing_xdc(clk_mhz: float) -> str:
    period = 1000.0 / clk_mhz
    return ("# primary clock constraint (exported)\n"
            "create_clock -period %.3f -name clk [get_ports clk]\n"
            % period)


# ----------------------------------------------------------------------
# verification vectors (stimulus + golden expected)
# ----------------------------------------------------------------------

def write_tb_vectors(cfg: FFTConfig, outdir: str, num_frames: int,
                     seed: int) -> None:
    """stimulus.txt / expected.txt exactly as the test-suite flow writes
    them (fft_gen.generate / generate_ssr semantics)."""
    import random
    from stimuli import random_frame
    from golden import OrderedFFTModel

    def markers(n_pts, frames):
        return [(1 if j == 0 else 0, 1 if j == n_pts - 1 else 0)
                for _ in range(frames) for j in range(n_pts)]

    rng = random.Random(seed)
    N = cfg.num_points
    frames = [random_frame(N, cfg.sample_width, rng) for _ in range(num_frames)]

    if cfg.ssr > 1:
        if cfg.input_order == "bitreversed":
            # P8 4b corner-order IFFT: the transpose-route model
            from golden_ssr import SSRCornerInverseModel as _PROBE
        else:
            from golden_ssr import SSRGoldenModel as _PROBE
        # pad frames let the pipeline fill before emission syncs -- same
        # arithmetic as fft_gen.generate_ssr so the vectors match 1:1
        M = N // cfg.ssr
        probe = _PROBE(cfg, arch=cfg.stage_mode)
        pad = (probe.latency + M - 1) // M + 2
        frames += [random_frame(N, cfg.sample_width, rng)
                   for _ in range(pad)]
        if cfg.input_order == "bitreversed":
            # slot e must carry the natural frame's sample bitrev_N(e)
            br = [int(format(k, "0%db" % cfg.num_stages)[::-1], 2)
                  for k in range(N)]
            samples = [fr[br[j]] for fr in frames for j in range(N)]
        else:
            samples = [s for fr in frames for s in fr]
        mk = []
        for _ in range(len(frames)):
            mk += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]
        with open(os.path.join(outdir, "stimulus.txt"), "w") as f:
            for (re_, im_), (u, l) in zip(samples, mk):
                f.write("%s %s %d %d\n" % (_hexw(re_, cfg.sample_width),
                                           _hexw(im_, cfg.sample_width), u, l))
        got = probe.process_stream(samples, markers=mk)
        exp_path = os.path.join(outdir, "expected.txt")
    else:
        samples = [s for fr in frames for s in fr]
        if cfg.is_dit:
            br = [int(format(k, "0%db" % cfg.num_stages)[::-1], 2)
                  for k in range(N)]
            samples = [fr[br[j]] for fr in frames for j in range(N)]
        mk = markers(N, num_frames)
        with open(os.path.join(outdir, "stimulus.txt"), "w") as f:
            for (re_, im_), (u, l) in zip(samples, mk):
                f.write("%s %s %d %d\n" % (_hexw(re_, cfg.sample_width),
                                           _hexw(im_, cfg.sample_width), u, l))
        if cfg.stage_mode == "r23":
            from golden import R23ChainGoldenModel
            m = R23ChainGoldenModel(cfg)
        elif cfg.is_r22:
            from golden import R22SDFGoldenModel
            m = R22SDFGoldenModel(cfg)
        else:
            m = OrderedFFTModel(cfg)
        got = m.process_stream(samples, markers=mk)
        exp_path = os.path.join(outdir, "expected.txt")

    with open(exp_path, "w") as f:
        for row in got:
            f.write("%d %d %d %d\n" % (row[0], row[1], row[2], row[3]))


def _hexw(v: int, width: int) -> str:
    return format(v & ((1 << width) - 1), "0%dx" % ((width + 3) // 4))


# ----------------------------------------------------------------------
# Shipped comparator (written to compare.py in every exported tree).
#
# The testbenches deliberately do NOT self-check: they stream stimulus.txt
# through the DUT and dump actual.txt, because the single source of truth is
# the golden model. That is only safe if the tree also ships the comparison
# rule -- otherwise "ok: N samples" (which any completed run prints) reads
# like a pass. Implements exactly the suite's rule:
#   * word-offset alignment (the RTL emits pipeline-fill words first)
#   * values within the documented tolerance (0 for R = 1 = bit-exact;
#     R/2 + 1 LSB for SSR, from the double quantization: lanes snap to
#     output_width, then the crossbar re-quantizes after its rounding shift)
#   * tuser/tlast compared POSITIONALLY and exactly (a count-only check
#     cannot see marker skew -- that is how the SSR marker bug survived)
COMPARE_PY = '''#!/usr/bin/env python3
"""fftgen exported-core self-check: expected.txt (golden model) vs actual.txt.

Usage (from this directory, after building and running the simulation from
README.txt):

    python3 compare.py

Exit status 0 = pass. The testbench itself only writes actual.txt, so THIS
is the check that decides whether the netlist is correct.
"""
import os
import sys


def read(path):
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(tuple(int(x) for x in ln.split()))
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    par = {}
    with open(os.path.join(here, "params.txt")) as f:
        for ln in f:
            if "=" in ln:
                k, v = ln.split("=", 1)
                par[k.strip()] = v.strip()
    N = int(par["num_points"])
    R = int(par.get("ssr", "1") or 1)
    # SSR quantizes twice (lane output, then the crossbar rescale); R = 1 is
    # bit-exact and must stay that way.
    tol = int(par.get("tolerance")
                 or (0 if R == 1 else R // 2 + 1))

    exp = read(os.path.join(here, "expected.txt"))
    act = read(os.path.join(here, "actual.txt"))
    if not exp or not act:
        print("FAIL: expected.txt and actual.txt must both be non-empty")
        return 1

    def close(e, a):
        return all(abs(x - y) <= tol for x, y in zip(e[:2], a[:2]))

    # the RTL emits pipeline-fill words before its stream locks to the frame
    # grid: find the word offset at which the whole remaining stream matches
    base = None
    for skip_w in range(0, len(act) // R):
        b = skip_w * R
        n_cmp = min(len(exp), len(act) - b)
        if n_cmp < N:
            break
        if all(close(exp[i], act[b + i]) for i in range(n_cmp)):
            base = b
            break
    if base is None:
        print("FAIL: no word-offset alignment found (values do not match the "
              "golden model at any frame offset)")
        for i in range(min(len(exp), len(act))):
            if not close(exp[i], act[i]):
                print(f"  first difference at line {i}: expected {exp[i]}, "
                      f"got {act[i]}")
                break
        return 1

    n_cmp = min(len(exp), len(act) - base)
    bad = [i for i in range(n_cmp) if not close(exp[i], act[base + i])]
    mk = [i for i in range(n_cmp)
          if exp[i][2:] != act[base + i][2:]]
    worst = max((max(abs(exp[i][0] - act[base + i][0]),
                     abs(exp[i][1] - act[base + i][1]))
                 for i in range(n_cmp)), default=0)
    print(f"num_points={N} ssr={R} tolerance={tol} aligned at word offset "
          f"{base // R}, comparing {n_cmp} samples")
    print(f"  values : max |delta| = {worst} (tolerance {tol}), "
          f"{len(bad)} violations")
    print(f"  markers: {len(mk)} tuser/tlast position mismatches")
    if bad or mk:
        for i in (bad or mk)[:5]:
            print(f"  line {base + i}: expected {exp[i]}, got {act[base + i]}")
        return 1
    print("PASS: bit-exact within the documented contract "
          "(values within tolerance, markers positional)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def export(cfg: FFTConfig, args) -> dict:
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(os.path.join(outdir, "vivado"), exist_ok=True)
    written = []

    # RTL sources
    for fn in core_files(cfg):
        shutil.copy(os.path.join(RTL, fn), os.path.join(outdir, fn))
        written.append(fn)

    # twiddle ROM contents
    from fft_gen import (write_twiddle_mem, write_lane_twiddle_mem,
                         write_wn_mem, write_preload_pack_vh,
                         write_r22_twiddle_mem, write_w1_inv_mem)
    if cfg.stage_mode == "r23":
        # 2 x M-point lane ROMs (M-point table) + the crossbar WN ROM
        # (N-point table); R=1: the same lane ROMs ARE the core's ROMs
        from datasheet_sweep import _r23_lane_roms
        from twiddles import canonical_twiddles
        TWm = cfg.twiddle_width
        mask = (1 << TWm) - 1
        m = cfg.num_points // cfg.ssr
        twm = canonical_twiddles(m, TWm, cfg.twiddle_decimal, cfg.inverse)
        _r23_lane_roms(m, twm, mask, outdir)
        written += ["fft_tw_r23_t0.mem", "fft_tw_r23_t1.mem",
                    "fft_tw_r23_t2.mem", "fft_tw_r22_l.mem"]
        if cfg.ssr > 1:
            write_wn_mem(cfg, os.path.join(outdir, "fft_wn.mem"))
            written.append("fft_wn.mem")
    elif cfg.is_r22:
        # R=1 r22: one pair-sliced ROM; SSR r22: per-lane r22 ROM +
        # the crossbar WN table (corner-inverse: the wrapper's W_N^{-p}
        # row instead -- no crossbar)
        if cfg.ssr == 1:
            write_r22_twiddle_mem(cfg, os.path.join(
                outdir, "fft_twiddles_r22.mem"))
            written.append("fft_twiddles_r22.mem")
        else:
            lc = lane_cfg(cfg)
            lc.input_order = "native"
            lc.output_order = "bitreversed"
            write_r22_twiddle_mem(lc, os.path.join(
                outdir, "fft_twiddles_r22_lane.mem"))
            written.append("fft_twiddles_r22_lane.mem")
            if cfg.input_order == "bitreversed":
                write_w1_inv_mem(cfg, os.path.join(outdir, "fft_w1_inv.mem"))
                written.append("fft_w1_inv.mem")
            else:
                write_wn_mem(cfg, os.path.join(outdir, "fft_wn.mem"))
                written.append("fft_wn.mem")
    else:
        write_twiddle_mem(cfg, os.path.join(outdir, "fft_twiddles.mem"))
        written.append("fft_twiddles.mem")
        if cfg.ssr > 1:
            lc = lane_cfg(cfg)
            write_lane_twiddle_mem(lc, os.path.join(outdir,
                                                    "fft_twiddles_lane.mem"))
            write_wn_mem(cfg, os.path.join(outdir, "fft_wn.mem"))
            written += ["fft_twiddles_lane.mem", "fft_wn.mem"]

    # post-warm FSM preload pack (single source: golden stage_preloads).
    # r22 needs none: pair phases are K_PRELOAD localparams and the
    # leftover's parity state is computed inside fft_sdf_r22.v
    import dataclasses
    from golden import SDFGoldenModel
    if cfg.is_r22 or cfg.stage_mode == "r23":
        # r23: per-stage K_PRELOAD localparams (like r22), no FSM preloads
        nbits = 0
    else:
        if cfg.ssr > 1:
            lc = lane_cfg(cfg)
            lc.input_order = "native"
            lc.output_order = "bitreversed"
            gm = SDFGoldenModel(lc, dit=False)
        else:
            nat_in, nat_out = (("bitreversed", "native") if cfg.is_dit
                               else ("native", "bitreversed"))
            gm = SDFGoldenModel(dataclasses.replace(
                cfg, input_order=nat_in, output_order=nat_out),
                dit=cfg.is_dit)
        nbits = write_preload_pack_vh(
            gm.stage_preloads, os.path.join(outdir, "fft_preloads.vh"))
        written.append("fft_preloads.vh")

    # generated wrapper (R=1 only; SSR keeps documented -G generics)
    if cfg.ssr == 1:
        with open(os.path.join(outdir, "fft_core.v"), "w") as f:
            f.write(core_wrapper_r22(cfg) if cfg.is_r22
                    else core_wrapper_r23(cfg) if cfg.stage_mode == "r23"
                    else core_wrapper_r1(cfg))
        written.append("fft_core.v")
    else:
        # SSR needs the GENERIC per-lane wrapper (instantiated by the
        # SSR top); r22 lanes use fft_top_r22. r23 lanes are whole
        # fft_sdf_r23 cores -- no lane wrapper at all.
        if not cfg.stage_mode == "r23":
            lane_top = "fft_top_r22.v" if cfg.is_r22 else "fft_top.v"
            shutil.copy(os.path.join(RTL, lane_top),
                        os.path.join(outdir, lane_top))
            if lane_top not in written:
                written.append(lane_top)
        # bake the SSR top's parameters (the Vivado flow passed them as
        # -generic overrides; bake so any synthesis flow -- Quartus, LSE,
        # yosys -- sees the configured core, not the module defaults)
        ssr_top = ("fft_ssr_r22_inv.v" if cfg.input_order == "bitreversed"
                   else "fft_ssr_r22.v" if cfg.is_r22 else
                   "fft_ssr_r23.v" if cfg.stage_mode == "r23" else "fft_ssr.v")
        bake_ssr_wrapper(cfg, os.path.join(outdir, ssr_top),
                        os.path.join(outdir, ssr_top))

    # records
    with open(os.path.join(outdir, "fft_params.vh"), "w") as f:
        f.write(params_header(cfg))
    written.append("fft_params.vh")

    extra = {}
    extra["latency"] = latency_cycles(cfg)
    extra["frames_per_clock_den"] = cfg.num_points // cfg.ssr
    if cfg.is_r22:
        npairs = cfg.num_stages // 2
        if cfg.ssr > 1:
            extra["dsp"] = ("%d est (R lanes x 4 x %d pairs; crossbar "
                            "network on top; last-pair D=1 included)"
                            % (cfg.ssr * 4 * max(1, lane_cfg(cfg).num_stages // 2),
                               lane_cfg(cfg).num_stages // 2))
        else:
            extra["dsp"] = "%d est (4 per stage pair)" % (4 * npairs)
    elif cfg.stage_mode == "r23":
        # 4 DSPs per r23 triple and per r22 leftover pair (one shared
        # complex multiplier each), +4 for the R=2 crossbar
        from datasheet_sweep import _r23_supported
        mm = cfg.num_points // cfg.ssr
        ntrip = _r23_supported(mm)
        npairl = (mm.bit_length() - 1 - 3 * ntrip) // 2
        per = 4 * (ntrip + npairl)
        extra["dsp"] = ("%d est (%d lanes x %d + crossbar %d)"
                        % (cfg.ssr * per + (4 if cfg.ssr > 1 else 0),
                           cfg.ssr, per, 4 if cfg.ssr > 1 else 0))
    elif cfg.ssr > 1:
        n_st = lane_cfg(cfg).num_stages
        extra["dsp"] = ("%d (R lanes x 4 x %d stages + crossbar network)"
                        % (cfg.ssr * 4 * n_st, n_st))
    else:
        extra["dsp"] = "%d (4 per stage)" % (4 * cfg.num_stages)
    extra["preload_bits"] = nbits
    with open(os.path.join(outdir, "params.txt"), "w") as f:
        f.write(params_txt(cfg, extra))
    written.append("params.txt")

    with open(os.path.join(outdir, "twiddle_map.txt"), "w") as f:
        f.write(twiddle_map_txt(cfg))
    written.append("twiddle_map.txt")

    # verification vectors + testbench
    if args.num_frames > 0:
        write_tb_vectors(cfg, outdir, args.num_frames, args.seed)
        written += ["stimulus.txt", "expected.txt"]
        os.makedirs(os.path.join(outdir, "tb"), exist_ok=True)
        tbname = ("tb_fft_ssr_r22_inv.cpp"
                  if cfg.input_order == "bitreversed" else
                  "tb_fft_ssr_r22.cpp" if (cfg.is_r22 and cfg.ssr > 1) else
                  "tb_fft_ssr_r23.cpp" if (cfg.stage_mode == "r23"
                                           and cfg.ssr > 1) else
                  "tb_fft_ssr.cpp" if cfg.ssr > 1 else "tb_fft_sdf.cpp")
        shutil.copy(os.path.join(TB, tbname),
                    os.path.join(outdir, "tb", tbname))
        written.append("tb/" + tbname)

    # vivado scripts
    with open(os.path.join(outdir, "vivado", "synth.tcl"), "w") as f:
        f.write(synth_tcl(cfg, args.part, args.clk_mhz))
    with open(os.path.join(outdir, "vivado", "timing.xdc"), "w") as f:
        f.write(timing_xdc(args.clk_mhz))
    written += ["vivado/synth.tcl", "vivado/timing.xdc"]

    # the verification rule has to be EXECUTABLE, not prose: the shipped
    # testbench only writes actual.txt and prints 'ok: N samples' for any
    # run that completed, so a customer following README.txt alone could
    # take a 97%-wrong netlist (e.g. a mis-set REORDER_OUT) as a pass.
    with open(os.path.join(outdir, "compare.py"), "w") as f:
        f.write(COMPARE_PY)
    written.append("compare.py")

    with open(os.path.join(outdir, "README.txt"), "w") as f:
        f.write(readme_txt(cfg, args))
    written.append("README.txt")

    return {"outdir": outdir, "files": written}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--num-points", type=int, required=True,
                    help="N, power of two (>= 2)")
    ap.add_argument("--ssr", type=int, default=1,
                    help="samples per clock, power of two dividing N")
    ap.add_argument("--inverse", action="store_true", help="IFFT")
    ap.add_argument("--input-order", default="native",
                    choices=["native", "bitreversed"])
    ap.add_argument("--output-order", default=None,
                    choices=["native", "bitreversed"],
                    help="default: bitreversed (DIF core convention)")
    ap.add_argument("--sample-width", type=int, default=16)
    ap.add_argument("--sample-decimal", type=int, default=0)
    ap.add_argument("--output-width", type=int, default=None,
                    help="defaults to sample-width")
    ap.add_argument("--output-decimal", type=int, default=None,
                    help="defaults to sample-decimal")
    ap.add_argument("--twiddle-width", type=int, default=18)
    ap.add_argument("--stage-mode", default="r2", choices=["r2", "r22", "r23"],
                    help="'r2' plain radix-2 SDF; 'r22' radix-2^2 folded "
                         "(P7: ~half the DSPs, 1-2 LSB contract delta); "
                         "'r23' radix-2^3 folded (S7: 3 samples/pass)")
    ap.add_argument("--scaling", default="auto",
                    help="'auto' or comma-separated per-stage shifts 0..2")
    ap.add_argument("--num-frames", type=int, default=3,
                    help="verification frames to emit (0 skips the TB)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--part", default="xcku5p-ffva676-1-e")
    ap.add_argument("--clk-mhz", type=float, default=500.0)
    ap.add_argument("--outdir", default=os.path.join("build", "export"))
    args = ap.parse_args(argv)
    if args.output_order is None:
        args.output_order = "bitreversed"

    try:
        cfg = build_config(args)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    res = export(cfg, args)
    print("Exported %s to %s/" % (
        "SSR%d" % cfg.ssr if cfg.ssr > 1 else "R=1", res["outdir"]))
    for name in res["files"]:
        print("  %s" % name)
    print("\nSee README.txt for simulation and synthesis commands.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
