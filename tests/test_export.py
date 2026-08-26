"""P5b export-flow tests: the exported deliverable set must build and
reproduce the golden vectors bit-exactly (R=1) / within documented SSR
tolerance, using ONLY the files and commands written by export_core.py.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("command failed (%d): %s\n%s"
                             % (r.returncode, " ".join(cmd), r.stderr[-2000:]))


def _export(args):
    from export_core import export, build_config
    import argparse
    ns = argparse.Namespace(
        num_points=args["num_points"], ssr=args.get("ssr", 1),
        inverse=args.get("inverse", False),
        input_order=args.get("input_order", "native"),
        output_order=args["output_order"],
        sample_width=16, sample_decimal=0,
        output_width=None, output_decimal=None,
        twiddle_width=18, scaling="auto",
        num_frames=args.get("num_frames", 3), seed=1,
        part="xcku5p-ffva676-1-e", clk_mhz=500.0,
        outdir=args["outdir"])
    cfg = build_config(ns)
    res = export(cfg, ns)
    return cfg, res


def _verilator_run(outdir, cfg):
    """Build + run the exported tree exactly as README.txt documents."""
    ssr = cfg.ssr > 1
    top = "fft_ssr" if ssr else "fft_top"
    files = (([] if ssr else ["fft_core.v"]) +
             ["fft_ssr.v", "fft_top.v", "fft_sdf.v", "fft_reorder.v",
              "fft_cross.v"][:4 if not ssr else 5])
    # keep order deterministic and explicit per arch
    files = (([] if ssr else ["fft_core.v"]) +
             (["fft_ssr.v", "fft_top.v", "fft_sdf.v", "fft_reorder.v",
               "fft_cross.v"] if ssr else
              ["fft_sdf.v", "fft_reorder.v"]))
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", top, "-Wno-fatal",
           "+define+FFTGEN_PRELOADS", "+incdir+.",
           "-GNUM_POINTS=%d" % cfg.num_points,
           "-GSCALING_PACK=32'h%08x" % (_pack(cfg)),
           "-GINTERN_WIDTH=%d" % (_iw(cfg)),
           "-GPIPE_DEPTH=10",
           "-CFLAGS",
           "-DTB_SAMPLE_WIDTH=%d -DTB_OUTPUT_WIDTH=%d -DTB_SSR=%d"
           % (cfg.sample_width, cfg.output_width, cfg.ssr),
           *files,
           os.path.join("tb", "tb_fft_ssr.cpp" if ssr
                        else "tb_fft_sdf.cpp")]
    if ssr:
        cmd[cmd.index("-GPIPE_DEPTH=10"):cmd.index("-GPIPE_DEPTH=10")] = [
            "-GSSR=%d" % cfg.ssr, "-GINVERSE=%d" % int(cfg.inverse),
        ]
    _run(cmd, outdir)
    _run([os.path.join("obj_dir", "V" + top)], outdir)


def _pack(cfg):
    from export_core import engine_cfg, scaling_pack
    return scaling_pack(engine_cfg(cfg))


def _iw(cfg):
    from export_core import engine_cfg, intern_width
    return intern_width(engine_cfg(cfg))


class TestExportR1(unittest.TestCase):
    """R=1 exports must reproduce the golden vectors bit-exactly."""

    def _check(self, args):
        outdir = os.path.join(ROOT, "build", args["name"])
        args["outdir"] = outdir
        cfg, res = _export(args)
        self.assertTrue(os.path.isfile(os.path.join(outdir, "fft_core.v")))
        self.assertTrue(os.path.isfile(
            os.path.join(outdir, "fft_params.vh")))
        self.assertTrue(os.path.isfile(
            os.path.join(outdir, "twiddle_map.txt")))
        _verilator_run(outdir, cfg)
        exp = [l.split() for l in open(os.path.join(outdir, "expected.txt"))
               if l.strip()]
        act = [l.split() for l in open(os.path.join(outdir, "actual.txt"))
               if l.strip()]
        self.assertEqual(len(exp), len(act))
        for e, a in zip(exp, act):
            self.assertEqual(e, a)

    def test_export_native_to_bitrev(self):
        self._check({"num_points": 64, "output_order": "bitreversed",
                     "name": "export_tst_n64"})

    def test_export_native_to_native(self):
        # exercises the generated reorder path in fft_core.v
        self._check({"num_points": 32, "output_order": "native",
                     "name": "export_tst_n32ro"})


class TestExportSSR(unittest.TestCase):
    """SSR exports ship the same vectors as generate_ssr and the tree
    runs within the documented R/2+1 LSB tolerance."""

    def test_export_ssr_r2(self):
        from config import FFTConfig
        from fft_gen import generate_ssr
        outdir = os.path.join(ROOT, "build", "export_tst_ssr")
        args = {"num_points": 32, "ssr": 2, "output_order": "native",
                "num_frames": 4, "outdir": outdir}
        cfg, res = _export(args)
        # artifacts identical to the suite flow (same seed)
        ref = os.path.join(ROOT, "build", "export_tst_ssr_ref")
        generate_ssr(FFTConfig(num_points=32, ssr=2,
                               output_order="native"),
                     ref, num_frames=4, seed=1)
        for fn in ("stimulus.txt", "expected.txt", "fft_preloads.vh"):
            with open(os.path.join(ref, fn)) as f1, \
                    open(os.path.join(outdir, fn)) as f2:
                self.assertEqual(f1.read(), f2.read(), fn)
        # run the exported tree
        _verilator_run(outdir, cfg)
        exp = [tuple(int(x) for x in l.split())
               for l in open(os.path.join(outdir, "expected.txt"))
               if l.strip()]
        act = [tuple(int(x) for x in l.split())
               for l in open(os.path.join(outdir, "actual.txt")) if l.strip()]
        tol = cfg.ssr // 2 + 1

        def ok(e, a):
            return all(abs(x - y) <= tol for x, y in zip(e[:2], a[:2]))

        aligned = None
        for skip in range(len(act) // cfg.ssr):
            base = skip * cfg.ssr
            n_cmp = min(len(exp), len(act) - base)
            if n_cmp < cfg.num_points:
                break
            if all(ok(exp[i], act[base + i]) for i in range(n_cmp)):
                aligned = base
                break
        self.assertIsNotNone(aligned, "exported SSR stream did not align")
        self.assertEqual(aligned, 0)


if __name__ == "__main__":
    unittest.main()
