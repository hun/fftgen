import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from golden import fft_float_reference, fft_float_radix2

import numpy as np
HAS_NUMPY = True


def rand_complex(n, rng):
    return [complex(rng.uniform(-100, 100), rng.uniform(-100, 100))
            for _ in range(n)]


class TestFloatReference(unittest.TestCase):
    def test_direct_matches_radix2(self):
        import random
        rng = random.Random(1)
        for N in (2, 4, 8, 16, 32):
            x = rand_complex(N, rng)
            a = fft_float_reference(x)
            b = fft_float_radix2(x)
            for va, vb in zip(a, b):
                self.assertLess(abs(va - vb), 1e-9 * max(1.0, abs(vb)))

    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_matches_numpy_forward(self):
        import random
        rng = random.Random(2)
        for N in (2, 8, 64, 256):
            x = rand_complex(N, rng)
            ref = np.fft.fft(np.array(x))
            ours = fft_float_radix2(x)
            err = max(abs(a - b) for a, b in zip(ours, ref))
            scale = max(abs(v) for v in ref)
            self.assertLess(err / scale, 1e-10, N)

    @unittest.skipUnless(HAS_NUMPY, "numpy not available")
    def test_matches_numpy_inverse(self):
        import random
        rng = random.Random(3)
        for N in (4, 16, 128):
            x = rand_complex(N, rng)
            ref = np.fft.ifft(np.array(x)) * N   # our convention: no 1/N
            ours = fft_float_radix2(x, inverse=True)
            err = max(abs(a - b) for a, b in zip(ours, ref))
            scale = max(abs(v) for v in ref)
            self.assertLess(err / scale, 1e-10, N)


if __name__ == "__main__":
    unittest.main()
