"""Roadmap phase 1.2 — honest toolpath preview of stored sheet programs.

- Parser: cut/rapid polylines, plunge stats, in-material rapid risk flag,
  arc sampling, fail-closed on anything outside the verified dialect.
- API: GET /cnc/api/sheet-preview/<plan_id>/ serves toolpaths parsed from
  the SAME stored program the download serves; unverified/cancelled/empty
  plans are fail-closed 409, outsiders 403.
- Page: the «مسیرها» button only appears for plans with verified output.
"""
import json
import math
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import CncFile, CncPartProfile
from . import gcode_sheet
from .sheet_adapter import build_sheet_program
from .sheet_nesting import plan_cnc_with_report
from .sheet_preview import SheetPreviewError, sheet_toolpaths
from .test_manual_queue import MINIMAL_1890, ManualQueueFixtureMixin


def fake_plan(gcode, width=2440.0, height=1220.0, parts=(), adapter='minimal_xy_router'):
    return SimpleNamespace(pk=7, gcode=gcode, width=width, height=height,
                           layout={'parts': list(parts), 'adapter': adapter})


class SheetToolpathParserTests(TestCase):
    def test_rectangle_contour_lengths_and_stats(self):
        program = build_sheet_program([
            {'name': 'A', 'text': MINIMAL_1890, 'place_x': 10, 'place_y': 10, 'rotated': False},
        ])
        data = sheet_toolpaths(fake_plan(program, parts=[
            {'x': 10, 'y': 10, 'width': 90, 'height': 1890, 'label': 'P1', 'rotated': False}]))
        self.assertEqual(data['tools'], [2])
        self.assertEqual(data['tool_changes'], [])
        self.assertEqual(data['stats']['plunges'], 1)
        self.assertEqual(data['stats']['depths'], [-12.0])
        self.assertEqual(data['stats']['risks'], 0)
        expected_lines = sum(
            1 for raw in program.splitlines()
            if (lambda l: l and l != '%' and l not in ('M2', 'M02', 'M30'))(
                gcode_sheet._clean(raw)))
        self.assertEqual(data['stats']['lines'], expected_lines)
        # the body cuts one 90 x 1890 rectangle -> 2 * (1890 + 90)
        self.assertAlmostEqual(data['stats']['cut_mm'], 2 * (1890 + 90), delta=0.5)
        cut_paths = [path for path in data['paths'] if path['k'] == 'cut']
        self.assertEqual(len(cut_paths), 1)
        self.assertEqual(cut_paths[0]['tool'], 2)
        self.assertAlmostEqual(cut_paths[0]['pts'][0][0], cut_paths[0]['pts'][-1][0], places=2)
        self.assertAlmostEqual(cut_paths[0]['pts'][0][1], cut_paths[0]['pts'][-1][1], places=2)
        self.assertEqual(data['boxes'][0]['label'], 'P1')
        self.assertEqual(data['adapter'], 'minimal_xy_router')

    def test_two_placements_merge_shared_rapid_and_keep_cut_separate(self):
        program = build_sheet_program([
            {'name': 'A', 'text': MINIMAL_1890, 'place_x': 10, 'place_y': 10, 'rotated': False},
            {'name': 'B', 'text': MINIMAL_1890, 'place_x': 200, 'place_y': 10, 'rotated': False},
        ])
        data = sheet_toolpaths(fake_plan(program))
        self.assertAlmostEqual(data['stats']['cut_mm'], 2 * 2 * (1890 + 90), delta=1.0)
        cut_paths = [path for path in data['paths'] if path['k'] == 'cut']
        self.assertEqual(len(cut_paths), 2)
        # part2's positioning rapid starts where part1's contour ended, so it
        # is contiguous with the CUT path — contiguity forbids merging it into
        # the first rapid polyline across the cut geometry -> two rapids.
        rapid_paths = [path for path in data['paths'] if path['k'] == 'rapid']
        self.assertEqual(len(rapid_paths), 2)
        self.assertGreater(data['stats']['rapid_mm'], 190)
        self.assertEqual(data['stats']['risks'], 0)

    def test_rotated_placement_keeps_length_and_flags_box(self):
        program = build_sheet_program([
            {'name': 'R', 'text': MINIMAL_1890, 'place_x': 20, 'place_y': 20, 'rotated': True},
        ])
        data = sheet_toolpaths(fake_plan(program, parts=[
            {'x': 20, 'y': 20, 'width': 90, 'height': 1890, 'label': 'PR', 'rotated': True}]))
        self.assertAlmostEqual(data['stats']['cut_mm'], 2 * (1890 + 90), delta=0.5)
        self.assertTrue(data['boxes'][0]['rotated'])

    def test_xy_rapid_at_negative_z_is_flagged_as_risk(self):
        program = (
            "%\nN10G21G90G54\nN20G53 Z0\nN30T2M6\nN40 M141\nN50 M6T2\nN60 G43 H2\n"
            "N70G0X10Y10Z5.000\nN80G1Z-6.000F9000\n"
            "N90G1X50Y10\nN100G0X80Y10\nN110G1X120Y10\n"
            "N130G0Z5.000\nN140G53Z0\nN150G53X0Y3950\nM30\n%\n"
        )
        data = sheet_toolpaths(fake_plan(program))
        self.assertEqual(data['stats']['risks'], 1)
        risk_paths = [path for path in data['paths'] if path.get('risk')]
        self.assertEqual(len(risk_paths), 1)
        self.assertEqual(risk_paths[0]['k'], 'rapid')
        self.assertEqual(risk_paths[0]['z'], -6.0)

    def test_arc_is_sampled_into_the_cut_path(self):
        program = (
            "%\nN10G21G90G54\nN20G53 Z0\nN30T2M6\nN40 M141\nN50 M6T2\nN60 G43 H2\n"
            "N70G0X10Y10Z5.000\nN80G1Z-3.000F9000\n"
            "N90G1X30Y10\nN100G3X50Y30I0J20\n"
            "N130G0Z5.000\nN140G53Z0\nN150G53X0Y3950\nM30\n%\n"
        )
        data = sheet_toolpaths(fake_plan(program))
        arc_cut = [path for path in data['paths'] if path['k'] == 'cut'][-1]
        self.assertGreater(len(arc_cut['pts']), 4)
        self.assertAlmostEqual(data['stats']['cut_mm'], 20 + 10 * math.pi, delta=1.0)

    def test_out_of_dialect_program_is_rejected(self):
        program = (
            "%\nN10G21G90G54\nN20G53 Z0\nN30T2M6\nN40 M141\nN50 M6T2\nN60 G43 H2\n"
            "N70G0X10Y10Z5.000\nN80G28\nM30\n%\n"
        )
        with self.assertRaises(SheetPreviewError):
            sheet_toolpaths(fake_plan(program))

    def test_empty_program_is_rejected(self):
        with self.assertRaises(SheetPreviewError):
            sheet_toolpaths(fake_plan(""))

    def test_inconsistent_arc_radius_is_rejected(self):
        program = (
            "%\nN10G21G90G54\nN20G53 Z0\nN30T2M6\nN40 M141\nN50 M6T2\nN60 G43 H2\n"
            "N70G0X10Y10Z5.000\nN80G1Z-3.000F9000\n"
            "N90G3X50Y30I0J99\n"
            "N130G0Z5.000\nN140G53Z0\nN150G53X0Y3950\nM30\n%\n"
        )
        with self.assertRaises(SheetPreviewError):
            sheet_toolpaths(fake_plan(program))


class SheetPreviewApiTests(ManualQueueFixtureMixin, TestCase):
    def make_verified_plan(self):
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm',
                               CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router'):
            plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        return plans[0]

    def get(self, plan_id):
        return self.client.get(reverse('cnc_resizer:sheet_preview', args=[plan_id]))

    def test_verified_plan_serves_toolpaths(self):
        plan = self.make_verified_plan()
        response = self.get(plan.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['plan_id'], plan.pk)
        self.assertTrue(data['filename'].endswith('.nc'))
        self.assertGreater(data['stats']['cut_mm'], 0)
        self.assertEqual(len(data['boxes']), 1)
        self.assertTrue(data['paths'])
        self.assertIn('material', data)

    def test_unverified_plan_is_fail_closed_409(self):
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)  # planned without adapter → no program
        response = self.get(plans[0].pk)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(json.loads(response.content)['ok'])

    def test_cancelled_plan_is_409(self):
        plan = self.make_verified_plan()
        plan.status = 'cancelled'
        plan.save(update_fields=['status'])
        self.assertEqual(self.get(plan.pk).status_code, 409)

    def test_other_user_without_manage_is_403(self):
        plan = self.make_verified_plan()
        outsider = User.objects.create_user('no-cnc-3', 'n3@example.test', 'pw')
        self.client.force_login(outsider)
        self.assertEqual(self.get(plan.pk).status_code, 403)

    def test_paths_button_only_on_verified_stage(self):
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        page = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertNotContains(page, 'data-paths-open')
        program = build_sheet_program([
            {'name': 'A', 'text': MINIMAL_1890, 'place_x': 10, 'place_y': 10, 'rotated': False},
        ])
        plan.gcode = program
        plan.output_verified = True
        plan.save(update_fields=['gcode', 'output_verified'])
        page = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertContains(page, 'data-paths-open')
        self.assertContains(page, reverse('cnc_resizer:sheet_preview', args=[plan.pk]))
