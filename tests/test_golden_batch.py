import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from config import FFTConfig
from golden import fft_fixed_batch, fft_float_radix2
from quant import complex_multiply_karatsuba, round_shift
from stimuli import impulse, random_frame, tone_frame
from twiddles import canonical_twiddles

import numpy as np
HAS_NUMPY = True


def to_complex(frame):
    return [complex(re, im) for re, im in frame]


def rand_complex(n, rng):
    return [complex(rng.uniform(-100, 100), rng.uniform(-100, 100))
            for _ in range(n)]


def bitrev_permute(seq):
    """Reorder seq[k] -> slot bitrev(k) (self-inverse permutation)."""
    N = len(seq)
    bits = N.bit_length() - 1
    out = [(0, 0)] * N
    for k, v in enumerate(seq):
        out[int(format(k, f"0{bits}b")[::-1], 2)] = v
    return out


def snr_db(ref, dut):
    """SNR of DUT against reference, in dB (error power vs signal power)."""
    sig = sum(abs(v) ** 2 for v in ref)
    err = sum(abs(r - d) ** 2 for r, d in zip(ref, dut))
    if err == 0:
        return float("inf")
    return 10 * math.log10(sig / err)


class TestBatchVsFloat(unittest.TestCase):
    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_auto_scaled_snr(self):
        # auto scaling removes log2(N) bits; quantized twiddles dominate the
        # error. Expect healthy SQNR for sane widths.
        rng = random.Random(11)
        for N in (8, 32, 128):
            cfg = FFTConfig(num_points=N, sample_width=16,
                            twiddle_width=18)
            frame = random_frame(N, 16, rng)
            dut = fft_fixed_batch(frame, cfg)
            # batch output is in bit-reversed SLOT order; compare against
            # natural-bin reference permuted the same way.
            ref = np.fft.fft(np.array(to_complex(frame))) / N
            ref = bitrev_permute([complex(v) for v in ref])
            self.assertGreater(snr_db(ref, to_complex(dut)), 55.0, N)

    def test_exact_mode_matches_float_times_N(self):
        # huge widths + zero scaling => only twiddle quantization remains.
        # With wide twiddles this must track the float result closely.
        rng = random.Random(12)
        for N in (8, 64):
            n = N.bit_length() - 1
            cfg = FFTConfig(num_points=N, sample_width=64, sample_decimal=8,
                            output_width=96, output_decimal=8,
                            twiddle_width=52, scaling=(0,) * n)
            frame = [(int(round(v.real * 256)), int(round(v.imag * 256)))
                     for v in rand_complex(N, rng)]
            dut = fft_fixed_batch(frame, cfg)
            # zero shifts => pipeline computes the raw integer spectrum
            # (twiddle Q-format normalized away by the td-shifts), emitted
            # in bit-reversed slot order.
            ref = bitrev_permute(fft_float_radix2(to_complex(frame)))
            self.assertGreater(snr_db(ref, to_complex(dut)), 90.0, N)

    def test_impulse_response_flat(self):
        # impulse of amplitude A: true spectrum is flat A; the scaled core
        # reports X_true/N = A/N at every slot (in input Q-format).
        N = 16
        cfg = FFTConfig(num_points=N, sample_width=16, twiddle_width=18)
        amp = 1 << 12
        out = fft_fixed_batch(impulse(N, re=amp), cfg)
        expected = amp >> cfg.num_stages          # A/N
        for slot, (re, im) in enumerate(out):
            self.assertEqual((re, im), (expected, 0), slot)

    def test_dc_input_amplitude_preserved(self):
        # DC of amplitude A: X[0] = A*N -> scaled output slot bitrev(0)=0
        # holds exactly A; every other slot ~ 0.
        N = 16
        cfg = FFTConfig(num_points=N, sample_width=16, twiddle_width=18)
        amp = 1 << 12
        frame = [(amp, 0)] * N
        out = fft_fixed_batch(frame, cfg)
        self.assertEqual(out[0], (amp, 0))
        for slot in range(1, N):
            self.assertLessEqual(abs(out[slot][0]), 2, slot)
            self.assertEqual(out[slot][1], 0)

    def test_tone_peaks_in_correct_bin(self):
        # output order is BIT-REVERSED: peak of bin b appears at slot
        # bitrev(b). Detailed sweep lives in the next test.
        N = 32
        cfg = FFTConfig(num_points=N, sample_width=16)
        frame = tone_frame(N, bin_index=5, amplitude=1 << 12)
        out = fft_fixed_batch(frame, cfg)
        slot = int(format(5, "05b")[::-1], 2)
        mags = [abs(complex(*v)) for v in out]
        self.assertEqual(max(range(N), key=lambda k: mags[k]), slot)

    def test_bitrev_output_order_property(self):
        # feeding a single-tone frame, the peak must appear at slot
        # bitrev(bin). Verify for several bins.
        N = 16
        cfg = FFTConfig(num_points=N, sample_width=16)
        for b in (0, 1, 3, 7, 15):
            frame = tone_frame(N, bin_index=b, amplitude=4096)
            out = fft_fixed_batch(frame, cfg)
            fmt = "{:04b}"
            slot = int(fmt.format(b)[::-1], 2)
            mags = [abs(complex(*v)) for v in out]
            peak = max(range(N), key=lambda k: mags[k])
            self.assertEqual(peak, slot, (b, slot))


class TestBatchRoundTrip(unittest.TestCase):
    def test_forward_then_inverse_recovers(self):
        # NOTE: both directions are DIF (native->bitrev). A streaming chain
        # fwd->inv therefore needs the DIT inverse (P3); here we compose the
        # permutations explicitly to validate the NUMERICS of the inverse.
        rng = random.Random(21)
        for N in (4, 16, 64):
            fwd = FFTConfig(num_points=N, sample_width=16, twiddle_width=18)
            inv = FFTConfig(num_points=N, sample_width=16, twiddle_width=18,
                            inverse=True)
            frame = random_frame(N, 16, rng)
            spec = fft_fixed_batch(frame, fwd)              # slots bitrev
            spec_nat = bitrev_permute(spec)                 # natural bins
            rec = fft_fixed_batch(spec_nat, inv)            # time, bitrev
            rec_nat = bitrev_permute(rec)                   # natural time
            # both directions carry conservative /N-type scaling: round
            # trip gain ~1/N. Accumulated rounding ~0.5-0.75 LSB per stage
            # per direction; allow 1.5*n total.
            worst = max(abs(r0 - f0 / N) + abs(r1 - f1 / N)
                        for (r0, r1), (f0, f1) in zip(rec_nat, frame))
            self.assertLess(worst, max(2.0, 1.5 * inv.num_stages), N)


class TestScalingBound(unittest.TestCase):
    def test_no_overflow_under_auto_schedule(self):
        # The conservative bound: every intermediate |component| <= max|x|.
        # Re-implement the stage loop here with explicit checking so the
        # bound is verified directly rather than trusted.
        rng = random.Random(31)
        for N in (8, 32, 64):
            cfg = FFTConfig(num_points=N, sample_width=16)
            shifts = cfg.shifts
            td = cfg.twiddle_decimal
            from twiddles import canonical_twiddles
            tw = canonical_twiddles(N, 18, td, False)
            x = [[re, im] for re, im in random_frame(N, 16, rng)]
            bound = max(max(abs(re), abs(im)) for re, im in x)
            for s in range(cfg.num_stages):
                D = N >> (s + 1)
                nxt = []
                for start in range(0, N, 2 * D):
                    for j in range(D):
                        i1, i2 = start + j, start + j + D
                        ar, ai = x[i1]
                        br, bi = x[i2]
                        su = (round_shift(ar + br, shifts[s]),
                              round_shift(ai + bi, shifts[s]))
                        dr, di = ar - br, ai - bi
                        cr, ci = tw[(j << s) % N]
                        mr, mi = complex_multiply_karatsuba(dr, di, cr, ci)
                        pr = (round_shift(mr, td + shifts[s]),
                              round_shift(mi, td + shifts[s]))
                        nxt.extend([su, pr])
                        for vr, vi in (su, pr):
                            self.assertLessEqual(max(abs(vr), abs(vi)), bound,
                                                 (N, s))
                x = nxt


if __name__ == "__main__":
    unittest.main()
