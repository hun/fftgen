import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig
from golden import SDFGoldenModel, fft_fixed_batch
from stimuli import freeze_mask, random_frame


def frames_concat(frames):
    return [smp for f in frames for smp in f]


def bitrev_index(k, N):
    bits = N.bit_length() - 1
    return int(format(k, f"0{bits}b")[::-1], 2)


class TestStreamVsBatchBitExact(unittest.TestCase):
    """THE core verification axis: streaming schedule == batch recursion."""

    def _check(self, cfg, num_frames, seed):
        rng = random.Random(seed)
        frames = [random_frame(cfg.num_points, cfg.sample_width, rng)
                  for _ in range(num_frames)]
        model = SDFGoldenModel(cfg)
        got = model.process_stream(frames_concat(frames))
        expected = []
        for f in frames:
            expected.extend(fft_fixed_batch(f, cfg))
        self.assertEqual(len(got), len(expected))
        for idx, (g, e) in enumerate(zip(got, expected)):
            self.assertEqual(g, e, f"sample {idx}: {g} != {e}")

    def test_matrix_sizes(self):
        for N in (2, 4, 8, 16, 32, 64):
            self._check(FFTConfig(num_points=N), num_frames=3, seed=N)

    def test_inverse(self):
        for N in (4, 16, 32):
            self._check(FFTConfig(num_points=N, inverse=True),
                        num_frames=2, seed=100 + N)

    def test_explicit_scaling_schedules(self):
        for shifts in [(0, 0, 0), (2, 0, 1), (1, 2, 0), (2, 2, 2)]:
            cfg = FFTConfig(num_points=8, scaling=shifts,
                            output_width=24)   # headroom for weak scaling
            with self.subTest(shifts=shifts):
                self._check(cfg, num_frames=3, seed=hash(shifts) & 0xffff)

    def test_width_variants(self):
        for sw, tw in ((8, 8), (12, 10), (16, 18), (25, 18)):
            cfg = FFTConfig(num_points=16, sample_width=sw,
                            output_width=sw + 8, twiddle_width=tw)
            with self.subTest(sw=sw, tw=tw):
                self._check(cfg, num_frames=2, seed=sw * 31 + tw)

    def test_large_N(self):
        for N in (128, 256):
            with self.subTest(N=N):
                self._check(FFTConfig(num_points=N), num_frames=2, seed=N)

    def test_long_run_periodicity(self):
        # 25 back-to-back frames: any phase drift between stage FSMs and
        # the frame period would desynchronize slot mapping.
        self._check(FFTConfig(num_points=8), num_frames=25, seed=777)

    def test_fuzz_random_configs(self):
        rng = random.Random(2024)
        for trial in range(20):
            N = 2 ** rng.randint(1, 7)              # 2 .. 128
            shifts = [rng.choice((0, 1, 2)) for _ in range(N.bit_length() - 1)]
            cfg = FFTConfig(
                num_points=N,
                inverse=rng.random() < 0.5,
                sample_width=rng.randint(6, 24),
                output_width=rng.randint(6, 28),
                twiddle_width=rng.randint(6, 18),
                scaling=shifts,
            )
            with self.subTest(trial=trial, cfg=repr(cfg)):
                self._check(cfg, num_frames=2, seed=1000 + trial)


class TestStreamFreezeSemantics(unittest.TestCase):
    def test_freeze_only_adds_latency(self):
        """Gapped enable must produce the dense run's values exactly."""
        cfg = FFTConfig(num_points=16)
        rng = random.Random(7)
        frame = random_frame(16, 16, rng)

        dense = SDFGoldenModel(cfg)
        dense_out = dense.process_stream(frame)

        for style in ("periodic", "bursty", "pseudo"):
            gated = SDFGoldenModel(cfg)
            # feed one sample per cycle, mask decides if the datapath runs;
            # a frozen cycle consumes no input sample.
            out = []
            idx = 0
            mask = freeze_mask(4096, seed=42, style=style)
            c = 0
            while idx < len(frame):
                en = mask[c % len(mask)]
                c += 1
                v, re, im, _, _ = gated.tick(en, frame[idx][0], frame[idx][1])
                if v:
                    out.append((re, im))
                if en:
                    idx += 1
            for _ in range(gated.latency - 1):
                v, re, im, _, _ = gated.tick(True, 0, 0)
                if v:
                    out.append((re, im))
            self.assertEqual(out, dense_out, style)


class TestStreamLatencyAndFraming(unittest.TestCase):
    def test_latency_constant(self):
        """First valid output after exactly `latency` enabled cycles."""
        for N in (2, 4, 8, 16):
            cfg = FFTConfig(num_points=N)
            m = SDFGoldenModel(cfg)
            rng = random.Random(N)
            frame = random_frame(N, 16, rng)
            first_valid = None
            n_feed = frame * (((m.latency + N - 1) // N) + 2)
            for i, smp in enumerate(n_feed):
                v, *_ = m.tick(True, smp[0], smp[1])
                if v and first_valid is None:
                    first_valid = i + 1     # 1-based tick index
                    break
            expected = m.latency
            self.assertEqual(first_valid, expected, f"N={N}")

    def test_frame_alignment_back_to_back(self):
        """Output slot j of frame f equals batch result of frame f."""
        cfg = FFTConfig(num_points=8)
        rng = random.Random(99)
        frames = [random_frame(8, 16, rng) for _ in range(5)]
        m = SDFGoldenModel(cfg)
        got = m.process_stream(frames_concat(frames))
        per_frame = len(got) // len(frames)
        self.assertEqual(per_frame, 8)
        for fidx, frame in enumerate(frames):
            exp = fft_fixed_batch(frame, cfg)
            chunk = got[fidx * 8:(fidx + 1) * 8]
            self.assertEqual(chunk, exp, f"frame {fidx}")

    def test_reset_recovers(self):
        cfg = FFTConfig(num_points=8)
        rng = random.Random(5)
        frame = random_frame(8, 16, rng)
        ref = SDFGoldenModel(cfg).process_stream(frame)

        m = SDFGoldenModel(cfg)
        m.process_stream(frame)             # run something
        m.tick(False)                       # idle cycles must be inert
        m.reset()                           # mid-stream reset
        again = m.process_stream(frame)
        self.assertEqual(again, ref)


class TestStreamRejectsUnbuiltConfigs(unittest.TestCase):
    def test_ssr_not_yet(self):
        # legal config (SSR native -> native), rejected by the R=1 model
        with self.assertRaises(NotImplementedError):
            SDFGoldenModel(FFTConfig(num_points=8, ssr=2,
                                     output_order="native"))

    def test_order_conversion_not_in_core(self):
        with self.assertRaises(NotImplementedError):
            SDFGoldenModel(FFTConfig(num_points=8, input_order="bitreversed"))
        with self.assertRaises(NotImplementedError):
            SDFGoldenModel(FFTConfig(num_points=8, output_order="native"))


if __name__ == "__main__":
    unittest.main()
