#!/usr/bin/env python3
"""One-shot deterministic SSR debug harness.

Regenerates the SSR build, compiles a probe binary with per-cycle
internal visibility, drives the golden model over the identical
stimulus, and produces an aligned comparison report:

  [1] lane streams : RTL lane outputs vs golden lane outputs
  [2] emissions    : RTL actual.txt vs golden emission stream
                     (block- and sample-granular offset search)
  [3] sync         : first-output cycle / phase on both sides
  [4] raw dump     : per-cycle crossbar internals for eyeballing

Usage:  python3 spikes/ssr_dbg.py [--n 8] [--r 2] [--frames 6] [--seed 1]
Everything is absolute-pathed and idempotent; safe to re-run.
"""
import argparse
import dataclasses
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

PROBE_MARKER = "// [SSRDBG] probe inserted"

LINE_PAT = re.compile(
    r"CYC j=(\d+) .*?l0=\((-?\d+),(-?\d+),(\d+),(\d+)\) "
    r".*?l1=\((-?\d+),(-?\d+),(\d+),(\d+)\) "
    r".*?p=(\d+) pd=(\d+) pd2=(\d+) pd3=(\d+) sc=(\d+) sy=(\d+) "
    r"v1=(\d+) v2=(\d+) vl=(\d+) "
    r"o=\((-?\d+),(-?\d+)\|(-?\d+),(-?\d+)\) uv=(\d+)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--r", type=int, default=2)
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    N, R, F, SEED = args.n, args.r, args.frames, args.seed

    from config import FFTConfig
    cfg = FFTConfig(num_points=N, ssr=R, output_order="native")
    SW = cfg.sample_width
    OW = cfg.output_width

    # ---- 1. regenerate -------------------------------------------------
    outdir = os.path.join(ROOT, "build", "ssrdbg")
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    from fft_gen import generate_ssr
    res = generate_ssr(cfg, outdir, num_frames=F, seed=SEED)
    print(f"[gen] rc={res['rc']} n_expected={res.get('n_expected')} "
          f"fb={res.get('first_bad')}")

    # ---- 2. inject per-cycle probe into local fft_cross.v copy --------
    xbp = os.path.join(outdir, "fft_cross.v")
    s = open(xbp).read()
    if PROBE_MARKER not in s:
        anchor = "        end else if (run) begin"
        idx = s.find(anchor)
        assert idx >= 0, "probe anchor"
        probe_line = ('            $display("[XB] p=%0d scnt=%0d sy=%u '
                      'v1=%u v2=%u vlast=%u");\n')
        ins = ('            // ' + PROBE_MARKER + '\n' +
               probe_line.split("\n")[0].split("// ")[0].join(["", ""]))
        # simple: insert a single display right after the branch opener
        ins = ('            // ' + PROBE_MARKER + '\n'
               '            if (v1 || v2 || synced)\n'
               '                $display("[XB] p=%0d scnt=%0d sy=%u '
               'v1=%u v2=%u vlast=%u", p, scnt, synced, v1, v2, vlast);\n')
        s = s[:idx] + ins + s[idx:]
        open(xbp, "w").write(s)
    print("[probe] injected")

    # ---- 3. compile probe binary ---------------------------------------
    return build_and_run(cfg, outdir, dict(F=F, R=R, SW=SW, OW=OW,
                                           N=N, SEED=SEED))


def build_and_run(cfg, outdir, P):
    F, R, SW, OW, N, SEED = P["F"], P["R"], P["SW"], P["OW"], P["N"], P["SEED"]
    cpp = open(os.path.join(HERE, "ssr_probe_template.cpp")).read()
    cpp = cpp.replace("@SW@", str(SW)).replace("@OW@", str(OW))
    cpp = cpp.replace("@R@", str(R)).replace("@F@", str(F * N + 60))
    pcpp = os.path.join(outdir, "probe.cpp")
    open(pcpp, "w").write(cpp)

    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", "fft_ssr", "--public-flat-rw", "-Wno-fatal",
           "-GNUM_POINTS=%d" % cfg.num_points, "-GSSR=%d" % R,
           "-GSAMPLE_WIDTH=%d" % cfg.sample_width,
           "-GSAMPLE_DECIMAL=%d" % cfg.sample_decimal,
           "-GOUTPUT_WIDTH=%d" % cfg.output_width,
           "-GOUTPUT_DECIMAL=%d" % cfg.output_decimal,
           "-GTWIDDLE_WIDTH=%d" % cfg.twiddle_width,
           "-GTWIDDLE_DECIMAL=%d" % cfg.twiddle_decimal,
           "-GSCALING_PACK=32'h%08x" % (sum((sh & 3) << (2 * s_)
               for s_, sh in enumerate(dataclasses.replace(
                   cfg, num_points=cfg.num_points // R, ssr=1).shifts)),
               ),
           "-GINTERN_WIDTH=%d" % (cfg.sample_width + max(0,
               dataclasses.replace(cfg, num_points=cfg.num_points // R,
                                   ssr=1).num_stages
               - sum(dataclasses.replace(cfg, num_points=cfg.num_points // R,
                                         ssr=1).shifts)) + 1),
           "+define+FFTGEN_PRELOADS", "+incdir+.",
           "fft_ssr.v", "fft_top.v", "fft_sdf.v", "fft_reorder.v",
           "fft_cross.v", "probe.cpp", "-o", "probe", "--Mdir", "prob"]
    r = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True)
    if r.returncode != 0:
        print("[probe-build] FAILED"); print(r.stderr[-1500:]); return 1
    print("[probe-build] ok")

    pr = subprocess.run([os.path.join("prob", "probe")],
                        cwd=outdir, capture_output=True, text=True)
    dump = [ln for ln in pr.stdout.splitlines() if ln.startswith("CYC")]
    print(f"[probe-run] {len(dump)} cycles")

    # ---- golden side -----------------------------------------------------
    samples = []; markers = []
    for ln in open(os.path.join(outdir, "stimulus.txt")):
        p_ = ln.split()
        if len(p_) == 4:
            re_v = int(p_[0], 16); im_v = int(p_[1], 16)
            if re_v >= 32768: re_v -= 65536
            if im_v >= 32768: im_v -= 65536
            samples.append((re_v, im_v)); markers.append((int(p_[2]), int(p_[3])))

    from golden import SDFGoldenModel
    from golden_ssr import SSRGoldenModel
    m = SSRGoldenModel(cfg)
    M = m.M; L = m.lanes[0].latency

    glanes = [[] for _ in range(R)]
    orig = [ln.tick for ln in m.lanes]

    def mk_spy(r_, f):
        def t(en, re, im, u, l):
            res = f(en, re, im, u, l)
            if res[0]:
                glanes[r_].append((res[1], res[2]))
            return res
        return t
    for r_ in range(R):
        setattr(m.lanes[r_], "tick", mk_spy(r_, orig[r_]))

    em = m.process_stream(samples, markers=markers)

    # ---- parse rtl dump --------------------------------------------------
    rtl_lanes = [[] for _ in range(R)]
    rtl_emit = []
    rtl_first_ov = None
    rtl_sync_cycle = None
    for ln in dump:
        mt = LINE_PAT.match(ln.strip())
        if not mt:
            continue
        g = [int(x) for x in mt.groups()]
        (j, l0re, l0im, l0u, l0v, l1re, l1im, l1u, l1v,
         pc, pd_, pd2_, pd3_, sc_, sy_, v1_, v2_, vl_,
         o00, o01, o10, o11, uv_) = g
        if l0v:
            rtl_lanes[0].append((l0re, l0im))
        if l1v:
            rtl_lanes[1].append((l1re, l1im))
        if uv_:
            rtl_emit.append((o00, o01, o10, o11, pc, j))
        if rtl_first_ov is None:
            rtl_first_ov = j
        if sy_ and rtl_sync_cycle is None:
            rtl_sync_cycle = j

    # ---- report ----------------------------------------------------------
    rep = []
    rep.append("=" * 72)
    rep.append(f"SSR DEBUG REPORT  N={cfg.num_points} R={R} frames={F} "
               f"seed={SEED}")
    rep.append(f"golden latency={m.latency} CB_LAT={m.CB_LAT} L_l={L} M={M}")
    rep.append("=" * 72)

    rep.append("\n--- [1] LANE STREAMS (RTL vs golden, per lane) ---")
    all_ok = True
    for r_ in range(R):
        g = glanes[r_]
        a = rtl_lanes[r_]
        bad = None
        for i in range(min(len(g), len(a))):
            if g[i] != a[i]:
                bad = (i, g[i], a[i]); break
        status = (f"MATCH ({len(g)})" if bad is None and len(g) == len(a)
                  else f"DIVERGE/LEN at {bad or ('len', len(g), len(a))}")
        if bad is not None or len(g) != len(a):
            all_ok = False
        rep.append(f"  lane {r_}: {status} (golden {len(g)}, rtl "
                   f"{len(a)})")
        if bad:
            lo = max(0, bad[0] - 2)
            rep.append(f"    context golden: {g[lo:bad[0]+3]}")
            rep.append(f"    context rtl   : {a[lo:bad[0]+3]}")

    rep.append("\n--- [2] EMISSION STREAMS ---")
    rep.append(f"  golden {len(em)} emissions, rtl {len(rtl_emit)}")
    best = []
    n_cmp = min(len(em), len(rtl_emit))
    for off in range(0, max(1, len(em) - len(rtl_emit) + 1)):
        err = sum(abs(em[off + i][0] - rtl_emit[i][0])
                  + abs(em[off + i][1] - rtl_emit[i][1])
                  for i in range(n_cmp))
        best.append((err, off))
    best.sort()
    err, off = best[0]
    rep.append(f"  best sample-offset: {off} "
               f"(total abs err {err}, per-sample "
               f"{err / max(1, len(rtl_emit)):.1f})")
    if err == 0:
        rep.append("  >>> STREAMS IDENTICAL <<<")
        all_ok = all_ok and True
    else:
        all_ok = False
        rep.append("  first mismatches at that offset:")
        shown = 0
        for i in range(len(rtl_emit)):
            e = em[off + i]; a = rtl_emit[i]
            if abs(e[0] - a[0]) > 2 or abs(e[1] - a[1]) > 2:
                rep.append(f"    i={i}: golden {e[:2]} rtl {a[:2]}")
                shown += 1
                if shown >= 5:
                    break

    rep.append("\n--- [3] SYNC POINTS ---")
    g_first = next((i for i, o in enumerate(em) if o[2] == 1), None)
    a_first = next((i for i, a in enumerate(rtl_emit) if a[4] == 1), None)
    rep.append(f"  golden: SOF emission #{g_first}")
    rep.append(f"  rtl:    first out_valid cycle j={rtl_first_ov}, "
               f"synced latch cycle j={rtl_sync_cycle}, "
               f"SOF emission #{a_first}")

    open(os.path.join(outdir, "report.txt"), "w").write("\n".join(rep))
    print("\n".join(rep))
    verdict = "ALL OK" if (all_ok and err == 0) else "MISMATCH FOUND"
    print(f"\nVERDICT: {verdict}")
    print(f"full report: {os.path.join(outdir, 'report.txt')}")
    return 0 if (all_ok and err == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
