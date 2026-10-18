import json
import unittest

from . import core


TAPERED = """%
G90
G0 X0 Y0
G1 X200 Y0
G1 X250 Y300
G1 X100 Y400
G1 X30 Y200
G1 X0 Y0
G0 X0 Y0
M30
%\n"""


class ViewOnlyTests(unittest.TestCase):
    def test_strict_processing_rejects_slanted_only_geometry(self):
        with self.assertRaises(core.CncError):
            core.process(TAPERED, {"mode": "equal", "delta": 0})

    def test_view_only_processing_preserves_text_and_ignores_delta(self):
        result = core.process(TAPERED, {"mode": "equal", "delta": -200, "allow_view_only": True})
        self.assertTrue(result["ok"])
        self.assertFalse(result["resizable"])
        self.assertEqual(result["delta"], 0.0)
        self.assertEqual(result["cnc_text"], TAPERED)
        self.assertEqual(result["original"], result["resized"])
        self.assertTrue(result["warnings"])

    def test_web_handler_allows_view_only_but_rejects_invalid_content(self):
        status, data = core.handle_request(json.dumps({"content": TAPERED, "params": {"delta": -20}}).encode())
        self.assertEqual(status, 200)
        self.assertFalse(data["resizable"])
        status, data = core.handle_request(json.dumps({"content": "hello world"}).encode())
        self.assertEqual(status, 400)

    def test_feature_inventory_is_tool_agnostic(self):
        result = core.analyze(TAPERED)
        self.assertTrue(result['ok'])
        self.assertTrue(result['features'])
        self.assertIn(result['features'][0]['kind'], {'raster_or_area', 'unknown'})

    def test_unapproved_profile_is_view_only(self):
        profile = {
            'axis': 'length',
            'reference_points': {'start': 0, 'end': 400},
            'features': {'feature-1': {'mode': 'view_only'}},
        }
        result = core.process(TAPERED, {'profile': profile, 'delta': 100})
        self.assertTrue(result['profile_mode'])
        self.assertFalse(result['resizable'])
        self.assertEqual(result['cnc_text'], TAPERED)
