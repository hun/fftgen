"""Bit-exact Python model of rtl/fft_cross.v (R<=4 path).

Cycle-accurate replica of the sequential logic, fed by the same lane
stream the golden model produces (verified bit-exact vs RTL lanes).
Compare its emission stream against the RTL TB's actual.txt.
"""
import sys
sys.path.insert(0, '/home/hannes/Projects/fftgen/src')
from twiddles import canonical_twiddles
from config import FFTConfig

def s(val, bits):
    """wrap to signed 'bits'-width"""
    m = 1 << bits
    v = val & (m - 1)
    return v - m if v >= (m >> 1) else v

CB_LAT_PY = 6


class CrossbarRTL:
    def __init__(self, N, R, ow=16, td=17, tw=18):
        self.N, self.R, self.M = N, R, N // R
        self.MW = max(1, (self.M - 1).bit_length())
        self.PW = ow + tw
        self.AW = self.PW + (R - 1).bit_length() + 2
        self.SX = (R - 1).bit_length()   # $clog2(R): R=2->1, R=4->2
        assert (1 << self.SX) == R or R == 2 or R == 4
        self.OW, self.TD = ow, td
        # WN rom rows r=0..R-1 (Q(td)), same file the generator writes
        cfg = FFTConfig(num_points=N, ssr=R, output_order='native')
        full = canonical_twiddles(N, tw, td, cfg.inverse)
        self.wn = [[full[(r * p) % N] for p in range(self.M)]
                   for r in range(R)]
        # registers (reset state)
        self.p = 0
        self.pd = [ (1 << self.MW) - 1 ] * 6      # pd..pd6 all-ones
        self.scnt = 0
        self.synced = False
        self.wq = [(0, 0)] * R
        self.d = [(0, 0)] * R
        self.pp = [(0, 0, 0, 0)] * R              # pp1..pp4 per lane
        self.b = [(0, 0)] * R
        self.h = [(0, 0)] * R
        self.x = [(0, 0)] * R
        self.dout = [(0, 0)] * R
        self.v1 = self.v2 = self.vlast = False

    def rshift(self, v, sh):
        if sh <= 0:
            return s(v, self.AW)
        return s((v + (1 << (sh - 1))) >> sh, self.AW)

    def rescale_sat(self, v):
        t = self.rshift(v, self.TD)
        lo, hi = -(1 << (self.OW - 1)), (1 << (self.OW - 1)) - 1
        if not (lo <= t <= hi):
            t = hi if t > 0 else lo
        return t

    def tick(self, din):
        """din: list of R (re,im) lane values currently on the inputs.
        Returns (out_valid, dout list of R (re,im))."""
        R, M = self.R, self.M
        run = True  # caller only calls on enabled cycles
        # ---- g_pre blocks (per lane) ----
        wq_new, d_new, pp_new = [], [], []
        for gp in range(R):
            wr_, wi_ = self.wn[gp][self.p]
            dr, di = din[gp]
            wq_new.append((wr_, wi_))
            d_new.append((dr, di))
            pr, pi = self.d[gp]
            qr, qi = self.wq[gp]
            if gp == 0:
                # W_0 = (1,0): pp1 = re<<td, pp3 = im<<td, pp2=pp4=0
                pp_new.append((s(pr << self.TD, self.PW), 0,
                               s(pi << self.TD, self.PW), 0))
            else:
                pp_new.append((s(pr * qr - 0, self.PW),
                               s(pr * qi, self.PW),
                               s(pi * qr, self.PW),
                               s(pi * qi, self.PW)))
        # ---- main block ----
        p_new = (self.p + 1) % (1 << self.MW)
        pd_new = [self.p] + self.pd[:5]
        scnt_new = self.scnt + 1
        b_new, h_new, dout_new = [], [], []
        for i in range(R):
            p1, p2, p3, p4 = self.pp[i]
            b_new.append((s(p1 - p4, self.AW), s(p2 + p3, self.AW)))
        if R == 2:
            h_new = [(s(self.b[0][0] + self.b[1][0], self.AW),
                      s(self.b[0][1] + self.b[1][1], self.AW)),
                     (s(self.b[0][0] - self.b[1][0], self.AW),
                      s(self.b[0][1] - self.b[1][1], self.AW))]
            # stage 3a: s_x rounding shift of PREVIOUS h
            x_new = [(self.rshift(self.h[q][0], self.SX),
                      self.rshift(self.h[q][1], self.SX)) for q in range(2)]
        else:
            hh = self.h
            x_new = [
                (self.rshift(hh[0][0] + hh[2][0], self.SX),
                 self.rshift(hh[0][1] + hh[2][1], self.SX)),
                (self.rshift(hh[1][0] + hh[3][1], self.SX),
                 self.rshift(hh[1][1] - hh[3][0], self.SX)),
                (self.rshift(hh[0][0] - hh[2][0], self.SX),
                 self.rshift(hh[0][1] - hh[2][1], self.SX)),
                (self.rshift(hh[1][0] - hh[3][1], self.SX),
                 self.rshift(hh[1][1] + hh[3][0], self.SX)),
            ]
            h_new = [(s(self.b[0][0] + self.b[2][0], self.AW),
                      s(self.b[0][1] + self.b[2][1], self.AW)),
                     (s(self.b[0][0] - self.b[2][0], self.AW),
                      s(self.b[0][1] - self.b[2][1], self.AW)),
                     (s(self.b[1][0] + self.b[3][0], self.AW),
                      s(self.b[1][1] + self.b[3][1], self.AW)),
                     (s(self.b[1][0] - self.b[3][0], self.AW),
                      s(self.b[1][1] - self.b[3][1], self.AW))]
        # stage 3b: rescale/sat of PREVIOUS x
        dout_new = [(self.rescale_sat(xv[0]), self.rescale_sat(xv[1]))
                    for xv in self.x]
        # valid chain -- mirror RTL combinational/sequential split:
        #   out_phase0(T) = mature(T) && pd5(T)==0      (comb, current regs)
        #   synced(T+1)   <= synced(T) || (v2(T) && out_phase0(T))
        #   out_valid(T+1)= vlast(T+1) && (synced(T+1) || out_phase0(T+1))
        op_T = (self.scnt > (CB_LAT_PY) ) and (self.pd[5] == 0)
        syncedn = self.synced or (self.v2 and op_T)
        out_valid = self.v2 and (syncedn or
                                 ((scnt_new > (CB_LAT_PY)) and (pd_new[5] == 0)))
        v1n, v2n, vlastn = True, self.v1, self.v2
        # commit
        self.wq, self.d, self.pp = wq_new, d_new, pp_new
        self.p, self.pd, self.scnt = p_new, pd_new, scnt_new
        self.b, self.h, self.x, self.dout = b_new, h_new, x_new, dout_new
        self.v1, self.v2, self.vlast = v1n, v2n, vlastn
        self.synced = syncedn
        return out_valid, self.dout


if __name__ == '__main__':
    import os
    root = '/home/hannes/Projects/fftgen'
    N, R, F, SEED = 8, 2, 6, 1
    bd = f'{root}/build/ssr/N{N}_R{R}_sw16ow16'
    os.chdir(bd)
    sys.path.insert(0, f'{root}/src')
    from golden_ssr import SSRGoldenModel
    cfg = FFTConfig(num_points=N, ssr=R, output_order='native')
    g = SSRGoldenModel(cfg)
    M = g.M

    # replicate generate_ssr stimulus exactly
    rng = __import__("random").Random(SEED)
    PF = (g.latency + M - 1) // M + 2
    frames = [[(rng.randint(-2**15, 2**15 - 1),
                rng.randint(-2**15, 2**15 - 1)) for _ in range(N)]
              for _ in range(F + PF)]
    samples = [sm for fr in frames for sm in fr]
    markers = []
    for f in range(F + PF):
        markers += [(1, 0)] + [(0, 0)] * (N - 2) + [(0, 1)]

    # golden lane spies + emission reference in one pass
    cap = [[] for _ in range(R)]
    orig = [ln.tick for ln in g.lanes]
    def mk_spy(r_, f_):
        def spy(en, re_, im_, u, l):
            o = f_(en, re_, im_, u, l)
            if en and o[0]:
                cap[r_].append((o[1], o[2]))
            return o
        return spy
    for r_ in range(R):
        g.lanes[r_].tick = mk_spy(r_, orig[r_])
    got = g.process_stream(samples, markers=markers)
    exp = [(x[0], x[1]) for x in got]

    # crossbar model fed with captured lane words
    L = min(len(c) for c in cap)
    xb = CrossbarRTL(N, R)
    emas = []
    for k in range(L):
        v, dout = xb.tick([cap[r_][k] for r_ in range(R)])
        if v:
            emas.append(dout)
    flat = [x for wd in emas for x in wd]

    tol = R // 2 + 1
    def close(a, b):
        return abs(a[0]-b[0]) <= tol and abs(a[1]-b[1]) <= tol

    print(f'lane caps {[len(c) for c in cap]}, py emissions {len(flat)}, golden {len(exp)}')
    best = None
    for off in range(-len(flat)+1, len(exp)):
        n_cmp = min(len(exp)-off, len(flat)) if off >= 0 else len(flat)+off
        if n_cmp < 8:
            continue
        mism = sum(1 for i in range(n_cmp)
                   if not close(exp[off+i] if off >= 0 else exp[i],
                                flat[i] if off >= 0 else flat[i-off]))
        if best is None or mism < best[0]:
            best = (mism, off, n_cmp)
    print(f'PY vs GOLDEN: offset {best[1]}: {best[0]}/{best[2]} mismatches')
    off = best[1]
    n_cmp = best[2]
    shown = 0
    for i in range(n_cmp):
        e = exp[off+i] if off >= 0 else exp[i]
        p = flat[i] if off >= 0 else flat[i-off]
        if not close(e, p) and shown < 8:
            print(f'  i={i}: py={p} exp={e} X')
            shown += 1
