"""CNC nesting on registered offcuts (پرت) — opt-in via ``use_offcuts``.

The default path must stay byte-identical to the historical behaviour:
full sheets only. When the flag is set, usable offcut batches
(``SheetStock.is_offcut``) join the candidate pool and are preferred
before full sheets (deterministic ``stock_sort_key`` with ``priority``).
"""
import os
import tempfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from cutting.models import SheetStock, SheetStockEvent
from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CncSheetPlan
from .services import register_base_file
from .sheet_nesting import create_cnc_sheet_plans, plan_cnc_with_report

# Same dialect-valid shape as MINIMAL_1890 (test_manual_queue) but with a
# small 90x200mm cutting body, so the piece fits a small registered offcut.
SMALL_200 = (
    "%\nN10G21G90G54\nN20G53 Z0\nN30T2M6\nN40 M141\nN50 M6T2\nN60 G43 H2\n"
    "N70G0X10Y10Z5.000M3 S16000\nN80G1Z-12.000F15000.0\n"
    "N90G1X10Y210\nN100G1X100Y210\nN110G1X100Y10\nN120G1X10Y10\n"
    "N130G0Z5.000M3 S16000\nN140G53Z0\nN150G53X0Y3950\nM30\n%\n"
)


class CncOffcutNestingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('cnc-offcut', 'o@example.test', 'pw')
        self.client.force_login(self.user)
        self.material = Material.objects.create(name='MDF پرت', thickness=Decimal('16.0'))
        customer = Customer.objects.create(name='مشتری پرت')
        self.order = Order.objects.create(customer=customer, status='planned')
        self.part = Part.objects.create(
            material=self.material, name='قطعه کوچک CNC', length=Decimal('200.0'), width=Decimal('90.0'),
            f3='small.cnc.200x90', routing_code='cnc.paint', turn=False,
        )
        self.task = ProductionTask.objects.create(
            order=self.order, part=self.part, station_name='cut', step_order=1, quantity=1, status='pending',
        )
        self.full = SheetStock.objects.create(
            material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'), quantity=1,
            created_by=self.user,
        )
        self.offcut = SheetStock.objects.create(
            material=self.material, width=Decimal('800.0'), height=Decimal('600.0'), quantity=1,
            is_offcut=True, created_by=self.user,
        )
        self.archive = tempfile.TemporaryDirectory()
        self.addCleanup(self.archive.cleanup)
        self.path = os.path.join(self.archive.name, 'small.cnc')
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write(SMALL_200)
        register_base_file(self.path, self.part.f3, self.user)

    def test_default_nesting_uses_full_sheets_only(self):
        plans, issues = create_cnc_sheet_plans([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].source_stock_id, self.full.pk)
        self.assertFalse(SheetStockEvent.objects.filter(stock=self.offcut, event_type='reserve').exists())

    def test_use_offcuts_places_piece_on_registered_offcut(self):
        plans, issues, report = plan_cnc_with_report([self.order.pk], self.user, use_offcuts=True)
        self.assertEqual([row['status'] for row in report], ['nested'])
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.source_stock_id, self.offcut.pk)
        self.assertTrue(SheetStockEvent.objects.filter(stock=self.offcut, event_type='reserve').exists())
        self.assertEqual(len(plan.layout['parts']), 1)

    def test_too_small_offcut_falls_back_to_full_sheet(self):
        self.offcut.delete()  # only an offcut too small for the piece remains
        SheetStock.objects.create(
            material=self.material, width=Decimal('100.0'), height=Decimal('100.0'), quantity=1,
            is_offcut=True, created_by=self.user,
        )
        plans, issues = create_cnc_sheet_plans([self.order.pk], self.user, use_offcuts=True)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].source_stock_id, self.full.pk)

    def test_offcut_reserved_by_cnc_plan_is_hidden_from_cutting_planner(self):
        plan_cnc_with_report([self.order.pk], self.user, use_offcuts=True)
        from cutting.views import _reserved_counts
        self.assertEqual(_reserved_counts([self.offcut.pk, self.full.pk])[self.offcut.pk], 1)
        self.assertEqual(_reserved_counts([self.offcut.pk, self.full.pk])[self.full.pk], 0)

    def test_completed_cnc_sheet_consumes_the_offcut_row(self):
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user, use_offcuts=True)
        plan = plans[0]
        # The production gate separately covers verified-output generation;
        # here we simulate the approved program so the consume branch runs.
        CncSheetPlan.objects.filter(pk=plan.pk).update(gcode='N10G21', output_verified=True)
        response = self.client.post(reverse('cnc_resizer:complete_sheet_plan', args=[plan.pk]), {'outcome': 'used'})
        self.assertEqual(response.status_code, 302)
        self.offcut.refresh_from_db()
        self.assertEqual(self.offcut.quantity, 0)
        self.assertFalse(self.offcut.active)
        self.assertTrue(
            SheetStockEvent.objects.filter(stock=self.offcut, event_type='consume', quantity_delta=-1).exists()
        )
