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
        stage_mode=args.get("stage_mode", "r2"),
        num_frames=args.get("num_frames", 3), seed=1,
        part="xcku5p-ffva676-1-e", clk_mhz=500.0,
        outdir=args["outdir"])
    cfg = build_config(ns)
    res = export(cfg, ns)
    return cfg, res


def _verilator_run(outdir, cfg):
    """Build + run the exported tree exactly as README.txt documents."""
    ssr = cfg.ssr > 1
    r22 = cfg.is_r22
    top = ("fft_ssr_r22_inv" if cfg.input_order == "bitreversed" else
           "fft_ssr_r22" if (ssr and r22) else "fft_ssr" if ssr
           else "fft_top")
    if ssr:
        files = (["fft_ssr_r22_inv.v", "fft_top_r22.v", "fft_sdf_r22.v",
                  "fft_stage_r22.v", "fft_sdf.v", "fft_reorder.v"]
                 if cfg.input_order == "bitreversed" else
                 ["fft_ssr_r22.v", "fft_top_r22.v", "fft_sdf_r22.v",
                  "fft_stage_r22.v", "fft_sdf.v", "fft_reorder.v",
                  "fft_cross.v"] if r22 else
                 ["fft_ssr.v", "fft_top.v", "fft_sdf.v", "fft_reorder.v",
                  "fft_cross.v"])
    else:
        files = (["fft_core.v", "fft_sdf_r22.v", "fft_stage_r22.v",
                  "fft_sdf.v"] if r22 else
                 ["fft_core.v", "fft_sdf.v", "fft_reorder.v"])
    tbname = ("tb_fft_ssr_r22_inv.cpp" if cfg.input_order == "bitreversed" else
              "tb_fft_ssr_r22.cpp" if (ssr and r22) else
              "tb_fft_ssr.cpp" if ssr else "tb_fft_sdf.cpp")
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "4",
           "--top-module", top, "-Wno-fatal",
           *([] if r22 else ["+define+FFTGEN_PRELOADS", "+incdir+."]),
           "-GNUM_POINTS=%d" % cfg.num_points,
           "-GSCALING_PACK=32'h%08x" % (_pack(cfg)),
           "-GINTERN_WIDTH=%d" % (_iw(cfg)),
           "-GPIPE_DEPTH=10",
           "-CFLAGS",
           "-DTB_SAMPLE_WIDTH=%d -DTB_OUTPUT_WIDTH=%d -DTB_SSR=%d"
           % (cfg.sample_width, cfg.output_width, cfg.ssr),
           *files,
           os.path.join("tb", tbname)]
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


class TestExportR22(unittest.TestCase):
    """P7: r22 R=1 exports must reproduce the re-pinned golden vectors
    bit-exactly from the shipped files alone."""

    def _check(self, args):
        outdir = os.path.join(ROOT, "build", args["name"])
        args["outdir"] = outdir
        cfg, res = _export(args)
        for fn in ("fft_core.v", "fft_params.vh", "twiddle_map.txt",
                   "fft_twiddles_r22.mem", "fft_sdf_r22.v",
                   "fft_stage_r22.v"):
            self.assertTrue(os.path.isfile(os.path.join(outdir, fn)), fn)
        # no preload pack ships with r22 (parity is computed in RTL)
        self.assertFalse(os.path.isfile(
            os.path.join(outdir, "fft_preloads.vh")))
        _verilator_run(outdir, cfg)
        exp = [l.split() for l in open(os.path.join(outdir, "expected.txt"))
               if l.strip()]
        act = [l.split() for l in open(os.path.join(outdir, "actual.txt"))
               if l.strip()]
        self.assertEqual(len(exp), len(act))
        for e, a in zip(exp, act):
            self.assertEqual(e, a)

    def test_export_r22(self):
        self._check({"num_points": 64, "output_order": "bitreversed",
                     "stage_mode": "r22", "name": "export_tst_r22_n64"})

    def test_export_r22_inverse(self):
        self._check({"num_points": 32, "output_order": "bitreversed",
                     "stage_mode": "r22", "inverse": True,
                     "name": "export_tst_r22_n32inv"})


class TestExportSSR22(unittest.TestCase):
    """P7: SSR r22 export ships the same vectors as generate_ssr and
    runs within the documented R/2+1 tolerance."""

    def test_export_ssr22_r2(self):
        from fft_gen import generate_ssr
        from config import FFTConfig
        outdir = os.path.join(ROOT, "build", "export_tst_ssr22")
        args = {"num_points": 32, "ssr": 2, "output_order": "native",
                "stage_mode": "r22", "num_frames": 4, "outdir": outdir}
        cfg, res = _export(args)
        ref = os.path.join(ROOT, "build", "export_tst_ssr22_ref")
        generate_ssr(FFTConfig(num_points=32, ssr=2,
                               output_order="native",
                               stage_mode="r22"),
                     ref, num_frames=4, seed=1)
        for fn in ("stimulus.txt", "expected.txt",
                   "fft_twiddles_r22_lane.mem", "fft_wn.mem"):
            with open(os.path.join(ref, fn)) as f1, \
                    open(os.path.join(outdir, fn)) as f2:
                self.assertEqual(f1.read(), f2.read(), fn)
        _verilator_run(outdir, cfg)
        exp = [tuple(int(x) for x in l.split())
               for l in open(os.path.join(outdir, "expected.txt"))
               if l.strip()]
        act = [tuple(int(x) for x in l.split())
               for l in open(os.path.join(outdir, "actual.txt"))
               if l.strip()]
        tol = cfg.ssr // 2 + 1
        aligned = None
        for skip in range(len(act) // cfg.ssr):
            base = skip * cfg.ssr
            n_cmp = min(len(exp), len(act) - base)
            if n_cmp < cfg.num_points:
                break
            if all(all(abs(x - y) <= tol for x, y in zip(e[:2], a[:2]))
                   for e, a in zip(exp[:n_cmp], act[base:base + n_cmp])):
                aligned = base
                break
        self.assertIsNotNone(aligned, "exported SSR r22 stream did not align")


class TestExportCornerOrder(unittest.TestCase):
    """P8: the corner-order export must build AND self-verify from the
    README command alone -- that command is the only place REORDER_OUT is
    carried, and the testbench cannot tell a wrong order from a right one
    (it prints 'ok: N samples' for any completed run). So this test runs the
    shipped README command and then the shipped compare.py, plus a negative
    control proving compare.py actually catches a mis-set generic."""

    def _readme_build(self, outdir):
        """The verilator command exactly as README.txt states it."""
        import re
        import shlex
        blk = open(os.path.join(outdir, "README.txt")).read() \
            .split("Simulation (Verilator)")[1]
        cmd = " ".join(blk[blk.index("verilator"):blk.index(".cpp") + 4]
                       .replace("\\\n", " ").split())
        args = [t.strip('"') for t in re.findall(r'"[^"]*"|\S+', cmd)
                if t != "\\"]
        return args

    def _check(self, N, out_order, expect_reorder):
        outdir = os.path.join(ROOT, "build", f"export_tst_corner_{out_order}")
        cfg, res = _export({"num_points": N, "ssr": 2,
                            "output_order": out_order, "stage_mode": "r22",
                            "num_frames": 2, "outdir": outdir})
        args = self._readme_build(outdir)
        self.assertIn(f"-GREORDER_OUT={expect_reorder}", args,
                      "README sim command lost the emission-order generic")
        _run([a for a in args if a != "--build"] + ["--build"], outdir)
        _run([os.path.join("obj_dir", "Vfft_ssr_r22")], outdir)
        c = subprocess.run([sys.executable, "compare.py"], cwd=outdir,
                           capture_output=True, text=True)
        return cfg, outdir, c

    def test_corner_order_ships_and_verifies(self):
        cfg, outdir, c = self._check(32, "bitreversed", 0)
        self.assertEqual(c.returncode, 0,
                         f"compare.py failed:\n{c.stdout}{c.stderr}")
        self.assertIn("PASS", c.stdout)
        # markers positionally exact is part of the shipped check now
        self.assertIn("0 tuser/tlast position mismatches", c.stdout)
        par = open(os.path.join(outdir, "params.txt")).read()
        self.assertIn("output_order", par)
        # P8 corner order removes the lane reorder buffers -> params says so
        self.assertIn("lane reorder: none", par)

    def test_sim_and_synth_agree_on_emission_order(self):
        # The README build command and vivado/synth.tcl must carry the SAME
        # REORDER_OUT. They used to disagree by omission (synth.tcl never
        # passed it), which ships a native-order netlist against
        # bitrev-order vectors -- invisible to the testbench.
        for out_order, want in (("bitreversed", "0"), ("native", "1")):
            with self.subTest(output_order=out_order):
                _, outdir, _ = self._check(32, out_order, want)
                sim = " ".join(self._readme_build(outdir))
                tcl = open(os.path.join(outdir, "vivado", "synth.tcl")).read()
                self.assertIn(f"-GREORDER_OUT={want}", sim)
                self.assertIn(f"-generic REORDER_OUT={want}", tcl,
                              "synth.tcl lost the emission-order generic")

    def test_native_order_still_ships_reorder(self):
        cfg, outdir, c = self._check(32, "native", 1)
        self.assertEqual(c.returncode, 0, f"{c.stdout}{c.stderr}")
        self.assertIn("lane reorder:", open(os.path.join(outdir, "params.txt")).read())
        self.assertNotIn("lane reorder: none",
                         open(os.path.join(outdir, "params.txt")).read())

    def test_comparator_catches_wrong_reorder_generic(self):
        # NEGATIVE CONTROL: build the corner-order tree with the WRONG
        # REORDER_OUT. The tb still prints 'ok: N samples'; compare.py must
        # fail. Without this, the shipped self-check is unproven.
        cfg, outdir, _ = self._check(32, "bitreversed", 0)
        args = self._readme_build(outdir)
        bad = [a.replace("-GREORDER_OUT=0", "-GREORDER_OUT=1") for a in args]
        self.assertNotEqual(bad, args, "generic not found to corrupt")
        _run([a for a in bad if a != "--build"] + ["--build"], outdir)
        _run([os.path.join("obj_dir", "Vfft_ssr_r22")], outdir)
        c = subprocess.run([sys.executable, "compare.py"], cwd=outdir,
                           capture_output=True, text=True)
        self.assertNotEqual(c.returncode, 0,
                            "wrong emission order passed the shipped check")
        self.assertIn("FAIL", c.stdout + c.stderr)


class TestExportCornerInverse(unittest.TestCase):
    """P8 4b: the corner-order IFFT export (bitrev -> native, R=2, r22)
    must ship the same vectors as generate_ssr, build from the README
    command alone, and self-verify BIT-EXACT (tolerance 0) via the shipped
    compare.py."""

    def test_export_corner_inverse_ships_and_verifies(self):
        from fft_gen import generate_ssr
        from config import FFTConfig
        N = 32
        outdir = os.path.join(ROOT, "build", "export_tst_corner_inv")
        args = {"num_points": N, "ssr": 2, "inverse": True,
                "input_order": "bitreversed", "output_order": "native",
                "stage_mode": "r22", "num_frames": 2, "outdir": outdir}
        cfg, res = _export(args)
        # artifacts identical to the suite flow (same seed)
        ref = os.path.join(ROOT, "build", "export_tst_corner_inv_ref")
        generate_ssr(FFTConfig(num_points=N, ssr=2, inverse=True,
                               input_order="bitreversed",
                               output_order="native", stage_mode="r22"),
                     ref, num_frames=2, seed=1)
        for fn in ("stimulus.txt", "expected.txt",
                   "fft_twiddles_r22_lane.mem", "fft_w1_inv.mem"):
            with open(os.path.join(ref, fn)) as f1, \
                    open(os.path.join(outdir, fn)) as f2:
                self.assertEqual(f1.read(), f2.read(), fn)
        # no crossbar ships with the transpose wrapper
        self.assertFalse(os.path.isfile(os.path.join(outdir, "fft_cross.v")))
        self.assertFalse(os.path.isfile(os.path.join(outdir, "fft_wn.mem")))
        # the documented comparison tolerance is 0 (bit-exact)
        par = open(os.path.join(outdir, "params.txt")).read()
        self.assertIn("tolerance           = 0", par)
        self.assertIn("fft_ssr_r22_inv", par)
        # build + run + self-verify from the shipped files alone
        _verilator_run(outdir, cfg)
        c = subprocess.run([sys.executable, "compare.py"], cwd=outdir,
                           capture_output=True, text=True)
        self.assertEqual(c.returncode, 0,
                         f"compare.py failed:\n{c.stdout}{c.stderr}")
        self.assertIn("PASS", c.stdout)
        self.assertIn("tolerance 0", c.stdout)
        self.assertIn("0 tuser/tlast position mismatches", c.stdout)
        # synth.tcl builds the SAME top with the wrapper's ROM generics
        tcl = open(os.path.join(outdir, "vivado", "synth.tcl")).read()
        self.assertIn("-top fft_ssr_r22_inv", tcl)
        self.assertIn('W1_FILE="fft_w1_inv.mem"', tcl)
        self.assertNotIn("REORDER_OUT", tcl)


if __name__ == "__main__":
    unittest.main()
