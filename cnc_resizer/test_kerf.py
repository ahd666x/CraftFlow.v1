"""Package C — kerf-bend mode: transverse serpentine raster regeneration.

Covers: serpentine detection (incl. split passes, no-op words, inter-band
connectors), pitch-locked regeneration that FILLS the solid middle zone,
exact span delta, identical pass sets across depth layers, N renumbering,
and the fail-closed rejections (non-uniform pitch, hole inside the field,
missing raster).
"""
from django.test import SimpleTestCase

from . import core


def _n(lines):
    return [f"N{10 * (i + 1)}{ln}" for i, ln in enumerate(lines)]


def _circle(cx, cy, r=2.0, z=-12.0, f=10000.0):
    return [
        f"G0X{cx - r:.3f}Y{cy:.3f}Z11.000M3 S16000",
        f"G1Z{z:.3f}F{f:.1f}",
        f"G3X{cx:.3f}Y{cy - r:.3f}I+{r:.3f}J-0.000",
        f"G3X{cx + r:.3f}Y{cy:.3f}I+0.000J+{r:.3f}",
        f"G3X{cx:.3f}Y{cy + r:.3f}I-{r:.3f}J+0.000",
        f"G3X{cx - r:.3f}Y{cy:.3f}I-0.000J-{r:.3f}",
        "G0Z11.000M3 S16000",
    ]


def _serp(depth, y_lists, pitch, x0, x1, feed=10000.0):
    """One serpentine block: G0 entry, plunge, alternating passes with pitch
    transitions inside each band and a plain connector between bands."""
    out = [f"G0X{x0:.3f}Y{y_lists[0][0]:.3f}Z11.000M3 S16000",
           f"G1Z{depth:.3f}F{feed:.1f}"]
    k = 0
    for bi, ys in enumerate(y_lists):
        if bi:
            out.append(f"Y{ys[0]:.3f}")          # inter-band connector (not the pitch)
        for y in ys:
            xt = x1 if k % 2 == 0 else x0
            out.append(("G1X" if k == 0 else "X") + f"{xt:.3f}")
            k += 1
            if y != ys[-1]:
                out.append(f"Y{round(y - pitch, 3):.3f}")
    out.append("G0Z11.000M3 S16000")
    return out


def build_kerf_file(bands=((1257.0, 935.0), (531.0, 223.0)), pitch=14.0,
                    hole_ys=(149.5, 1330.5), depths=(-7.0, -14.0),
                    with_raster=True):
    """Synthetic counterpart of the workshop kerf-bending panel:
    long grooves crossing the field, two serpentine depth layers with a solid
    middle, two hole rows outside the field, closed arc contour, G53 footer."""
    x0, x1 = 32.167, 447.513
    y_lists = []
    for y_top, y_bot in bands:
        cnt = int(round((y_top - y_bot) / pitch)) + 1
        y_lists.append([round(y_top - i * pitch, 3) for i in range(cnt)])
    lines = ["G90G21G54", "G53 Z0", "T3M6", "M141", "M6T3", "G43 H3"]
    lines += [  # long vertical grooves crossing the whole part (no inner vertices)
        "G0X37.167Y3.000Z11.000M3 S16000",
        "G1Z-7.000F10000.0",
        "G1X44.167Y3.000",
        "X44.167Y1477.000",
        "X37.167",
        "X37.167Y3.000",
        "G0Z11.000M3 S16000",
    ]
    if with_raster:
        for d in depths:
            lines += _serp(d, y_lists, pitch, x0, x1)
    for hy in hole_ys:
        for cx in (109.167, 259.167):
            lines += _circle(cx, hy)
    lines += ["M6T2", "G43 H2",
              "G0X26.167Y6.542Z11.000M3 S15000",
              "G1Z-14.000F12000.0",
              "G1X26.167Y1473.458",
              "G2X30.167Y1477.458I+4.000J+0.000",
              "G1X435.167",
              "G2X439.167Y1473.458I+0.000J-4.000",
              "G1Y6.542",
              "G2X435.167Y2.542I-4.000J+0.000",
              "G1X30.167",
              "G2X26.167Y6.542I+0.000J+4.000",
              "G0Z11.000M3 S15000",
              "G0X0.000Y0.000M3 S15000",
              "G53Z0",
              "G53X0Y3950",
              "M30"]
    return "\n".join(_n(lines)) + "\n"


def _pass_ys(text):
    """Pass Y levels per serpentine block of a program (detection-based)."""
    lines, _ = core.split_lines(text)
    blocks = core._detect_serpentine_blocks(lines)
    return [[p[0] for p in b['passes']] for b in blocks]


class KerfDetectionTests(SimpleTestCase):
    def test_detects_two_depth_blocks_with_two_bands(self):
        lines, _ = core.split_lines(build_kerf_file())
        blocks = core._detect_serpentine_blocks(lines)
        self.assertEqual(len(blocks), 2)
        for b in blocks:
            self.assertEqual(len(b['bands']), 2)
            self.assertAlmostEqual(b['pitch'], 14.0, places=3)
            self.assertEqual(len(b['passes']), 47)   # 24 + 23
            self.assertAlmostEqual(b['x_min'], 32.167, places=3)
            self.assertAlmostEqual(b['x_max'], 447.513, places=3)

    def test_split_pass_and_noop_words_are_tolerated(self):
        # mimic the workshop file: zero-length word inside the field and a
        # pass emitted as two collinear G1 segments
        text = build_kerf_file()
        lines = text.splitlines()
        out = []
        for ln in lines:
            out.append(ln)
            if ln.endswith("X447.513") and "G1X" in ln:
                out.append(ln.replace("G1X447.513", "X330.214"))  # bogus split
        lines2, _ = core.split_lines("\n".join(out) + "\n")
        blocks = core._detect_serpentine_blocks(lines2)
        self.assertEqual(len(blocks), 2)


class KerfResizeTests(SimpleTestCase):
    def test_zero_delta_fills_the_solid_middle(self):
        res = core.process(build_kerf_file(),
                           {'mode': 'kerf', 'delta': 0, 'tool_dia': 4.0})
        self.assertTrue(res['resizable'])
        self.assertEqual(res['kerf']['passes_before'], 47)
        self.assertEqual(res['kerf']['passes_after'], 74)      # (1034/14 -> 73 gaps)+1
        self.assertAlmostEqual(res['kerf']['remainder'], 0.0, places=3)
        self.assertAlmostEqual(res['length_after'] - res['length_before'], 0.0, places=3)
        ys = _pass_ys(res['cnc_text'])
        self.assertEqual(len(ys), 2)
        for block in ys:
            self.assertEqual(len(block), 74)
            gaps = [round(a - b, 6) for a, b in zip(block, block[1:])]
            self.assertTrue(all(abs(g - 14.0) < 1e-6 for g in gaps))   # uniform + continuous

    def test_grow_385mm_adds_passes_and_keeps_pitch(self):
        res = core.process(build_kerf_file(),
                           {'mode': 'kerf', 'delta': 385, 'tool_dia': 4.0})
        self.assertAlmostEqual(res['length_after'] - res['length_before'], 385.0, places=3)
        self.assertEqual(res['kerf']['passes_after'], 101)     # floor(1412/14)=100 gaps
        self.assertAlmostEqual(res['kerf']['remainder'], 7.0, places=3)
        self.assertAlmostEqual(res['kerf']['field'][0], 230.0, places=3)   # 223+7
        self.assertAlmostEqual(res['kerf']['field'][1], 1642.0, places=3)
        ys = _pass_ys(res['cnc_text'])
        for block in ys:
            gaps = [round(a - b, 6) for a, b in zip(block, block[1:])]
            self.assertTrue(all(abs(g - 14.0) < 1e-6 for g in gaps))
        # hole row below the field stays, the upper one rides with the top zone
        vec = core.extract_vectors(res['cnc_text'].splitlines(), 4.0)
        cys = sorted(round(h['cy'], 3) for h in vec['holes'])
        self.assertIn(149.5, cys)
        self.assertIn(1715.5, cys)

    def test_shrink_removes_passes_and_keeps_pitch(self):
        res = core.process(build_kerf_file(),
                           {'mode': 'kerf', 'delta': -112, 'tool_dia': 4.0})
        self.assertAlmostEqual(res['length_after'] - res['length_before'], -112.0, places=3)
        self.assertEqual(res['kerf']['passes_after'], 66)
        self.assertAlmostEqual(res['kerf']['remainder'], 0.0, places=3)
        for block in _pass_ys(res['cnc_text']):
            gaps = [round(a - b, 6) for a, b in zip(block, block[1:])]
            self.assertTrue(all(abs(g - 14.0) < 1e-6 for g in gaps))

    def test_non_multiple_delta_remainder_goes_below(self):
        res = core.process(build_kerf_file(),
                           {'mode': 'kerf', 'delta': -100, 'tool_dia': 4.0})
        self.assertAlmostEqual(res['length_after'] - res['length_before'], -100.0, places=3)
        self.assertEqual(res['kerf']['passes_after'], 66)      # field shrinks 112
        self.assertAlmostEqual(res['kerf']['remainder'], 12.0, places=3)
        self.assertAlmostEqual(res['kerf']['field'][0], 235.0, places=3)   # 223+12

    def test_depth_layers_stay_aligned_and_n_renumbered(self):
        res = core.process(build_kerf_file(),
                           {'mode': 'kerf', 'delta': 385, 'tool_dia': 4.0})
        ys = _pass_ys(res['cnc_text'])
        self.assertEqual(ys[0], ys[1])                          # both depths identical
        nums = [int(ln.split('N')[1].split('G')[0].split('X')[0].split('Y')[0].split('Z')[0].split('M')[0].split('S')[0].split('T')[0])
                for ln in res['cnc_text'].splitlines() if ln.startswith('N')]
        self.assertEqual(nums, [10 * (i + 1) for i in range(len(nums))])

    def test_warnings_carry_kerf_summary(self):
        res = core.process(build_kerf_file(),
                           {'mode': 'kerf', 'delta': 0, 'tool_dia': 4.0})
        self.assertTrue(any('kerf' in w for w in res['warnings']))
        self.assertEqual(res['gaps'], [])


class KerfFailClosedTests(SimpleTestCase):
    def test_non_uniform_pitch_is_rejected(self):
        text = build_kerf_file().replace("Y1229.000", "Y1228.000", 1)  # one gap 15mm
        with self.assertRaises(core.CncError):
            core.process(text, {'mode': 'kerf', 'delta': 0, 'tool_dia': 4.0})

    def test_hole_inside_field_is_rejected(self):
        text = build_kerf_file(hole_ys=(600.0, 1330.5))
        with self.assertRaises(core.CncError) as ctx:
            core.process(text, {'mode': 'kerf', 'delta': 0, 'tool_dia': 4.0})
        self.assertIn('سوراخ', str(ctx.exception))

    def test_missing_raster_is_rejected(self):
        text = build_kerf_file(with_raster=False)
        with self.assertRaises(core.CncError) as ctx:
            core.process(text, {'mode': 'kerf', 'delta': 0, 'tool_dia': 4.0})
        self.assertIn('سرپنتاین', str(ctx.exception))

    def test_other_geometry_inside_field_is_rejected(self):
        text = build_kerf_file().replace("X44.167Y1477.000", "X44.167Y900.000", 1)
        with self.assertRaises(core.CncError) as ctx:
            core.process(text, {'mode': 'kerf', 'delta': 0, 'tool_dia': 4.0})
        self.assertIn('داخل ناحیهٔ شیار', str(ctx.exception))

    def test_overshrink_is_rejected(self):
        with self.assertRaises(core.CncError):
            core.process(build_kerf_file(),
                         {'mode': 'kerf', 'delta': -5000, 'tool_dia': 4.0})


class LegacyModesUnaffectedTests(SimpleTestCase):
    def test_equal_mode_still_resizes_the_same_fixture(self):
        res = core.process(build_kerf_file(),
                           {'mode': 'equal', 'delta': 100, 'symmetric': True,
                            'tool_dia': 4.0})
        self.assertTrue(res['resizable'])
        self.assertAlmostEqual(res['length_after'] - res['length_before'], 100.0, places=2)
        self.assertEqual(len(res['gaps']), 3)

    def test_kerf_mode_rejected_value_is_still_guarded(self):
        with self.assertRaises(core.CncError):
            core.process(build_kerf_file(), {'mode': 'nope', 'delta': 0})
