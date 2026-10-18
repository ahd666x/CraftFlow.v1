import os
import tempfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from cutting.models import SheetStock
from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CncSheetPlan
from .services import register_base_file
from .sheet_nesting import create_cnc_sheet_plans
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
        register_base_file(SAMPLE, self.cnc_part.f3, self.user)

    def test_all_same_material_cnc_parts_share_one_complete_sheet_program(self):
        plans, issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(issues, [])
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(len(plan.layout["parts"]), 3)
        self.assertEqual(plan.gcode.count("; PART"), 3)
        self.assertEqual(plan.gcode.count("M30"), 1)
        self.assertEqual(plan.gcode.count("G53X0Y3950"), 1)

    def test_running_twice_does_not_plan_the_same_pieces_again(self):
        create_cnc_sheet_plans([self.order.pk], self.user)
        plans, _issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(plans, [])
        self.assertEqual(CncSheetPlan.objects.count(), 1)

    def test_cancel_releases_pieces_and_sheet(self):
        first, _ = create_cnc_sheet_plans([self.order.pk], self.user)
        response = self.client.post(reverse("cnc_resizer:cancel_sheet_plan", args=[first[0].pk]))
        self.assertEqual(response.status_code, 302)
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
        self.assertContains(page, "دانلود G-code کل ورق")

    def test_sheets_used_by_cnc_are_not_offered_to_the_standard_planner(self):
        create_cnc_sheet_plans([self.order.pk], self.user)
        from cutting.views import _reserved_counts
        self.assertEqual(_reserved_counts([self.stock.pk])[self.stock.pk], 1)
