"""Marker sideband (tuser/tlast), fractional-format and saturation coverage.

Closes the audit gaps found in the P1 review: frame markers were in the
interface contract but untested; fractional formats never went through the
stream path; freeze equivalence was single-size; saturation was implicit.
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig
from golden import SDFGoldenModel, fft_fixed_batch
from stimuli import freeze_mask, random_frame


class TestMarkerAlignment(unittest.TestCase):
    """tuser(0)=SOF / tlast=EOF must emerge attached to their sample."""

    def _marker_frames(self, N, num_frames):
        markers = []
        for f in range(num_frames):
            for j in range(N):
                markers.append((1 if j == 0 else 0, 1 if j == N - 1 else 0))
        return markers

    def test_markers_align_dense(self):
        rng = random.Random(3)
        for N in (4, 16, 64):
            cfg = FFTConfig(num_points=N)
            frames = [random_frame(N, 16, rng) for _ in range(4)]
            samples = [s for fr in frames for s in fr]
            m = SDFGoldenModel(cfg)
            out = m.process_stream(samples,
                                   markers=self._marker_frames(N, 4))
            self.assertEqual(len(out), len(samples))
            for fi in range(4):
                chunk = out[fi * N:(fi + 1) * N]
                self.assertEqual(chunk[0][2], 1, f"SOF frame {fi}, N={N}")
                self.assertEqual(chunk[-1][3], 1, f"EOF frame {fi}, N={N}")
                # exactly one SOF/EOF per frame
                self.assertEqual(sum(c[2] for c in chunk), 1)
                self.assertEqual(sum(c[3] for c in chunk), 1)

    def test_markers_align_under_freeze(self):
        """Gaps must not detach a marker from its sample."""
        N = 8
        cfg = FFTConfig(num_points=N)
        rng = random.Random(9)
        frames = [random_frame(N, 16, rng) for _ in range(3)]
        samples = [s for fr in frames for s in fr]
        markers = self._marker_frames(N, 3)

        m = SDFGoldenModel(cfg)
        mask = freeze_mask(len(samples) * 4 + 64, seed=5, style="pseudo")
        out = []
        idx = 0
        c = 0
        while idx < len(samples):
            en = mask[c % len(mask)]
            c += 1
            u, l = markers[idx]
            v, re, im, ou, ol = m.tick(en, samples[idx][0],
                                       samples[idx][1], u, l)
            if en:
                idx += 1
            if v:
                out.append((re, im, ou, ol))
        for _ in range(m.latency - 1):
            v, re, im, ou, ol = m.tick(True, 0, 0)
            if v:
                out.append((re, im, ou, ol))

        self.assertEqual(len(out), len(samples))
        for fi in range(3):
            chunk = out[fi * N:(fi + 1) * N]
            self.assertEqual(chunk[0][2], 1)
            self.assertEqual(chunk[-1][3], 1)

    def test_values_unchanged_with_markers(self):
        """Carrying markers must not perturb the numerical results."""
        cfg = FFTConfig(num_points=16)
        rng = random.Random(13)
        frames = [random_frame(16, 16, rng) for _ in range(2)]
        samples = [s for fr in frames for s in fr]

        plain = SDFGoldenModel(cfg).process_stream(samples)
        marked = SDFGoldenModel(cfg).process_stream(
            samples, markers=self._marker_frames(16, 2))
        for p, q in zip(plain, marked):
            self.assertEqual(p[:2] if isinstance(p, tuple) else p, q[:2])


class TestFractionalFormats(unittest.TestCase):
    def test_stream_matches_batch_with_fractions(self):
        rng = random.Random(41)
        for trial in range(10):
            N = 2 ** rng.randint(2, 6)
            sd = rng.randint(0, 4)
            od = rng.randint(max(0, sd - 3), sd + 2)
            cfg = FFTConfig(num_points=N,
                            sample_width=rng.randint(10, 20),
                            sample_decimal=sd,
                            output_width=rng.randint(12, 24),
                            output_decimal=od,
                            twiddle_width=rng.randint(10, 18))
            with self.subTest(trial=trial, cfg=repr(cfg)):
                frames = [random_frame(N, cfg.sample_width, rng)
                          for _ in range(2)]
                samples = [s for fr in frames for s in fr]
                got = SDFGoldenModel(cfg).process_stream(samples)
                expected = []
                for fr in frames:
                    expected.extend(fft_fixed_batch(fr, cfg))
                self.assertEqual(got, expected)


class TestFreezeMultiSize(unittest.TestCase):
    def test_freeze_equivalence_sizes(self):
        for N in (4, 8, 32):
            for style in ("periodic", "pseudo"):
                with self.subTest(N=N, style=style):
                    cfg = FFTConfig(num_points=N)
                    rng = random.Random(100 + N)
                    frame = random_frame(N, 16, rng)

                    dense = SDFGoldenModel(cfg).process_stream(frame * 2)

                    gated = SDFGoldenModel(cfg)
                    mask = freeze_mask(4096, seed=N, style=style)
                    out = []
                    idx = 0
                    c = 0
                    total = 2 * N
                    while idx < total:
                        en = mask[c % len(mask)]
                        c += 1
                        smp = frame[idx % N]
                        v, re, im, _, _ = gated.tick(en, smp[0], smp[1])
                        if en:
                            idx += 1
                        if v:
                            out.append((re, im))
                    for _ in range(gated.latency - 1):
                        v, re, im, _, _ = gated.tick(True, 0, 0)
                        if v:
                            out.append((re, im))
                    self.assertEqual(out, dense)


class TestOutputSaturation(unittest.TestCase):
    def test_full_scale_clips_to_extremes(self):
        # DC input at full scale into a deliberately narrow output:
        # slot bitrev(0) carries A (amplitude-preserving contract); other
        # slots ~0. With output_width too small for A, the value must clip
        # to the positive extreme -- never wrap negative.
        N = 8
        amp = (1 << 15) - 1                     # full-scale positive
        cfg = FFTConfig(num_points=N, sample_width=16, output_width=8)
        frame = [(amp, 0)] * N
        out = fft_fixed_batch(frame, cfg)
        lo, hi = -(1 << 7), (1 << 7) - 1
        for slot, (re, im) in enumerate(out):
            self.assertTrue(lo <= re <= hi, (slot, re))
            self.assertTrue(lo <= im <= hi, (slot, im))
        self.assertEqual(out[0][0], hi)         # clipped DC bin, no wrap

    def test_stream_saturates_identically(self):
        N = 8
        amp = (1 << 15) - 1
        cfg = FFTConfig(num_points=N, sample_width=16, output_width=8)
        frame = [(amp, 0)] * N
        batch = fft_fixed_batch(frame, cfg)
        stream = SDFGoldenModel(cfg).process_stream(frame * 2)[:N]
        self.assertEqual(stream, batch)


if __name__ == "__main__":
    unittest.main()
