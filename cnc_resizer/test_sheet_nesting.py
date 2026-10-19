import os
import tempfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from cutting.models import SheetStock
from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CncSheetPlan
from .services import register_base_file
from .sheet_nesting import create_cnc_sheet_plans, _source_geometry, _with_explicit_mm
from .tests import SAMPLE


class CncSheetNestingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("cnc-sheet", "s@example.test", "pw")
        self.client.force_login(self.user)
        self.material = Material.objects.create(name="MDF CNC تست", thickness=Decimal("16.0"))
        customer = Customer.objects.create(name="مشتری آزمون")
        self.order = Order.objects.create(customer=customer, status="planned")
        self.cnc_part = Part.objects.create(
            material=self.material, name="پنل CNC", length=Decimal("1740.0"), width=Decimal("400.0"),
            f3="panel.1740x400", routing_code="cnc.paint", turn=False,
        )
        self.plain_part = Part.objects.create(
            material=self.material, name="دیواره", length=Decimal("400.0"), width=Decimal("300.0"),
            f3="wall.400x300", routing_code="cut.paint", turn=True,
        )
        self.cnc_task = ProductionTask.objects.create(
            order=self.order, part=self.cnc_part, station_name="cut", step_order=1, quantity=3, status="pending",
        )
        self.plain_task = ProductionTask.objects.create(
            order=self.order, part=self.plain_part, station_name="cut", step_order=1, quantity=1, status="pending",
        )
        self.stock = SheetStock.objects.create(
            material=self.material, width=Decimal("1830.0"), height=Decimal("3660.0"), quantity=2,
            created_by=self.user,
        )
        self.archive = tempfile.TemporaryDirectory()
        self.addCleanup(self.archive.cleanup)
        with open(SAMPLE, encoding='utf-8-sig') as source:
            known_mm_program = source.read().replace('N10G90G54', 'N10G21G90G54', 1)
        self.known_mm_path = os.path.join(self.archive.name, 'known-mm.cnc')
        with open(self.known_mm_path, 'w', encoding='utf-8') as destination:
            destination.write(known_mm_program)
        register_base_file(self.known_mm_path, self.cnc_part.f3, self.user)

    def test_all_same_material_cnc_parts_share_one_layout_without_unverified_machine_output(self):
        plans, issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].details, {'stage': 'output_blocked', 'quantity': 3})
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(len(plan.layout["parts"]), 3)
        self.assertEqual(plan.gcode, '')
        self.assertFalse(plan.output_verified)
        self.assertGreater(plan.planned_area, 0)
        self.assertEqual(self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[plan.pk])).status_code, 409)
        from cutting.models import SheetStockEvent
        self.assertTrue(SheetStockEvent.objects.filter(cnc_plan=plan, event_type='reserve').exists())

    def test_source_without_explicit_millimetre_units_is_rejected(self):
        from .gcode_sheet import GcodeError
        with self.assertRaisesRegex(GcodeError, 'G21'):
            _source_geometry(SAMPLE)

    def test_g21_is_injected_before_first_xy_move_and_is_idempotent(self):
        text, injected = _with_explicit_mm("%\r\nN10G90G54\r\nN20G0X1Y1\r\nM30\r\n%\r\n")
        self.assertTrue(injected)
        self.assertEqual(text.splitlines()[2], 'G21')
        self.assertIn('N20G0X1Y1', text)
        declared, not_injected = _with_explicit_mm("%\nN10G21G90G54\nN20G0X1Y1\n")
        self.assertFalse(not_injected)
        self.assertEqual(declared, "%\nN10G21G90G54\nN20G0X1Y1\n")

    def test_implicit_mm_setting_normalises_source_and_plans(self):
        register_base_file(SAMPLE, self.cnc_part.f3, self.user)
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm'):
            plans, issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        pieces = plans[0].layout['parts']
        self.assertEqual(len(pieces), 3)
        self.assertTrue(all(piece['units_injected'] for piece in pieces))
        self.assertEqual(issues[0].details, {'stage': 'output_blocked', 'quantity': 3})

    def test_implicit_mm_leaves_known_mm_sources_untouched(self):
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm'):
            _text, injected, *_rest = _source_geometry(self.known_mm_path)
        self.assertFalse(injected)

    def test_implicit_mm_still_rejects_inch_and_relative_sources(self):
        from .gcode_sheet import GcodeError
        archive = tempfile.TemporaryDirectory()
        self.addCleanup(archive.cleanup)
        inch_path = os.path.join(archive.name, 'inch.cnc')
        with open(inch_path, 'w', encoding='utf-8') as stream:
            stream.write("%\nN10G20G90\nN20G0X1Y1\n")
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm'):
            with self.assertRaisesRegex(GcodeError, 'G20'):
                _source_geometry(inch_path)
        relative_path = os.path.join(archive.name, 'relative.cnc')
        with open(relative_path, 'w', encoding='utf-8') as stream:
            stream.write("%\nN10G91\nN20G0X1Y1\n")
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm'):
            with self.assertRaisesRegex(GcodeError, 'G91'):
                _source_geometry(relative_path)

    def test_source_failure_issue_is_not_duplicated_on_repeated_attempt(self):
        register_base_file(SAMPLE, self.cnc_part.f3, self.user)
        first, first_issues = create_cnc_sheet_plans([self.order.pk], self.user)
        second, second_issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual((first, second), ([], []))
        self.assertEqual(len(first_issues), 1)
        self.assertEqual(len(second_issues), 1)
        self.assertEqual(self.cnc_task.cnc_planning_issues.filter(status='open').count(), 1)

    def test_running_twice_does_not_plan_the_same_pieces_again(self):
        create_cnc_sheet_plans([self.order.pk], self.user)
        plans, _issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(plans, [])
        self.assertEqual(CncSheetPlan.objects.count(), 1)

    def test_cancel_releases_pieces_and_sheet(self):
        first, _ = create_cnc_sheet_plans([self.order.pk], self.user)
        response = self.client.post(reverse("cnc_resizer:cancel_sheet_plan", args=[first[0].pk]))
        self.assertEqual(response.status_code, 302)
        from cutting.models import SheetStockEvent
        self.assertTrue(SheetStockEvent.objects.filter(cnc_plan=first[0], event_type='reserve').exists())
        self.assertTrue(SheetStockEvent.objects.filter(cnc_plan=first[0], event_type='release').exists())
        again, _ = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].sheet_index, first[0].sheet_index + 1)  # unique per stock+user

    def test_planner_cnc_mode_removes_cnc_parts_from_cut_plan_and_shows_preview(self):
        response = self.client.post(reverse("cutting:planner"), {
            "action": "plan", "order_ids": [self.order.pk], "kerf": "4.0", "planning_mode": "cnc_sheets",
        })
        self.assertEqual(response.status_code, 302)
        from cutting.models import CutPlanTask
        planned_task_ids = set(CutPlanTask.objects.values_list("task_id", flat=True))
        self.assertIn(self.plain_task.pk, planned_task_ids)
        self.assertNotIn(self.cnc_task.pk, planned_task_ids)
        page = self.client.get(reverse("cnc_resizer:sheet_plans"))
        self.assertContains(page, "فایل دستگاه تولید نشده")
        plan = CncSheetPlan.objects.get()
        self.assertNotContains(page, reverse('cnc_resizer:download_sheet_plan', args=[plan.pk]))

    def test_sheets_used_by_cnc_are_not_offered_to_the_standard_planner(self):
        create_cnc_sheet_plans([self.order.pk], self.user)
        from cutting.views import _reserved_counts
        self.assertEqual(_reserved_counts([self.stock.pk])[self.stock.pk], 1)

    def test_sheet_adapter_produces_verified_machine_output(self):
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm',
                               CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router'):
            plans, issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(issues, [])
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertTrue(plan.output_verified)
        self.assertTrue(plan.gcode.startswith('%\r\nN10G90G54\r\n'))
        self.assertEqual(plan.layout.get('adapter'), 'minimal_xy_router')
        self.assertEqual(plan.gcode.count('M141'), 1)
        from .models import CncPlanningIssue
        self.assertEqual(CncPlanningIssue.objects.count(), 0)
        response = self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[plan.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'N40M141', response.content)
        page = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertContains(page, 'دریافت فایل دستگاه')
        self.assertNotContains(page, 'فایل دستگاه تولید نشده')

    def test_adapter_failure_falls_back_to_blocked_output(self):
        second_part = Part.objects.create(
            material=self.material, name="قطعه قوسی", length=Decimal("1740.0"), width=Decimal("60.0"),
            f3="panel.arc.r10", routing_code="cnc.paint", turn=False,
        )
        ProductionTask.objects.create(
            order=self.order, part=second_part, station_name="cut", step_order=2, quantity=1, status="pending",
        )
        archive = tempfile.TemporaryDirectory()
        self.addCleanup(archive.cleanup)
        r_arc_path = os.path.join(archive.name, 'panel.arc.r10.cnc')
        with open(r_arc_path, 'w', encoding='utf-8') as stream:
            stream.write(
                "%\nN10G90G54\nN20G53 Z0\nN30T5M6\nN40 M141\nN50 M6T5\nN60 G43 H5\n"
                "N70G0X10Y10Z5.000M3 S16000\nN80G1X50Y10F9000.0\n"
                "N90G2X60Y20R10.0\nN100G1X10Y10\n"
                "N110G0Z5.000M3 S16000\nN120G0X0.000Y0.000M3 S16000\n"
                "N130G53Z0\nN140G53X0Y3950\nN150M30\n%\n"
            )
        register_base_file(r_arc_path, second_part.f3, self.user)
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm',
                               CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router'):
            plans, issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertFalse(plan.output_verified)
        self.assertEqual(plan.gcode, '')
        self.assertEqual(len(issues), 2)
        from .models import CncPlanningIssue
        open_issues = CncPlanningIssue.objects.filter(status='open', details__stage='output_blocked')
        self.assertEqual(open_issues.count(), 2)
        self.assertTrue(all('R' in issue.reason for issue in open_issues))
        self.assertEqual(
            self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[plan.pk])).status_code, 409,
        )
