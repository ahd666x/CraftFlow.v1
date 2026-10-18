"""python -m unittest cnc_resizer.tests   (or  python manage.py test cnc_resizer)
Put a sample file next to this one as sample.cnc to run the file-based tests."""
import os
import unittest

from . import core

SAMPLE = os.path.join(os.path.dirname(__file__), "sample.cnc")


@unittest.skipUnless(os.path.exists(SAMPLE), "sample.cnc not found")
class ResizeTests(unittest.TestCase):
    def setUp(self):
        with open(SAMPLE, newline="") as f:
            self.text = f.read()

    def test_equal_split_total_and_width(self):
        r = core.process(self.text, {"mode": "equal", "delta": -150})
        self.assertAlmostEqual(r["delta"], -150, places=3)
        self.assertAlmostEqual(r["length_before"] + r["delta"], r["length_after"], places=2)

    def test_x_never_changes(self):
        r = core.process(self.text, {"mode": "proportional", "delta": 200})
        xs = lambda t: [l.split("X")[1].split("Y")[0] for l in t.splitlines() if "X" in l and "Y" in l and "G53" not in l]
        self.assertEqual(xs(self.text), xs(r["cnc_text"]))

    def test_manual_total_is_sum(self):
        r = core.process(self.text, {"mode": "manual", "manual": [0, 0, -80, 0, 0]})
        self.assertAlmostEqual(r["delta"], -80, places=3)

    def test_zero_delta_is_identity(self):
        r = core.process(self.text, {"mode": "equal", "delta": 0})
        self.assertEqual(r["cnc_text"], self.text)

    def test_errors(self):
        with self.assertRaises(core.CncError):
            core.process("hello")
        with self.assertRaises(core.CncError):
            core.process(self.text, {"delta": -5000})
        self.assertEqual(core.handle_request(b"junk")[0], 400)
