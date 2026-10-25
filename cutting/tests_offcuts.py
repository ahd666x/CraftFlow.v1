"""Offcut (پرت) reuse surfaces in the cutting planner.

Covers: the ``priority`` stock policy end-to-end, the planner radio default
driven by ``CuttingSettings.strategy``, and the ``cnc_use_offcuts`` flag
through both preview (JSON) and the real POST round-trip.
"""
import os
import tempfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from cnc_resizer.models import CncSheetPlan
from cnc_resizer.services import register_base_file
from cnc_resizer.test_cnc_offcuts import SMALL_200
from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CuttingSettings, CutPlan, SheetStock


class PlannerOffcutSurfaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('offcut-ui', 'uo@example.test', 'pw')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='کارگاه پرت')
        self.material = Material.objects.create(name='MDF پرت UI', thickness=Decimal('16.0'))
        self.part = Part.objects.create(
            material=self.material, name='دیواره', length=Decimal('400.0'), width=Decimal('300.0'),
            pname='کمد', routing_code='cut', turn=True,
        )
        self.order = Order.objects.create(customer=self.customer, status='planned')
        self.task = ProductionTask.objects.create(
            order=self.order, part=self.part, station_name='cut', step_order=1, quantity=1, status='pending',
        )
        # CNC-first sibling for cnc_sheets previews (no base file needed — the
        # preview only reads effective specs).
        self.cnc_part = Part.objects.create(
            material=self.material, name='قطعه CNC', length=Decimal('300.0'), width=Decimal('200.0'),
            pname='کمد', routing_code='cnc.paint', turn=False,
        )
        self.cnc_task = ProductionTask.objects.create(
            order=self.order, part=self.cnc_part, station_name='cut', step_order=2, quantity=1, status='pending',
        )

    def add_stock(self, *, width='1830.0', height='3660.0', offcut=False, priority=100):
        return SheetStock.objects.create(
            material=self.material, width=Decimal(width), height=Decimal(height), quantity=1,
            is_offcut=offcut, priority=priority, created_by=self.user,
        )

    def test_priority_policy_reaches_plan_and_overrides_offcut_preference(self):
        self.add_stock(priority=10)  # full sheet, lowest priority number = consumed first
        self.add_stock(width='500.0', height='400.0', offcut=True, priority=500)
        response = self.client.post(reverse('cutting:planner'), {
            'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0', 'stock_policy': 'priority',
        })
        self.assertEqual(response.status_code, 302)
        plan = CutPlan.objects.latest('pk')
        self.assertEqual(plan.stock_policy, 'priority')
        sheet = plan.planned_sheets.get()
        self.assertFalse(sheet.source_stock.is_offcut)  # priority beats offcut-first

    def test_default_radio_follows_cutting_settings(self):
        response = self.client.get(reverse('cutting:planner'))
        self.assertContains(response, 'value="offcuts_first" form="planning-form" checked')
        CuttingSettings.objects.update_or_create(pk=1, defaults={'strategy': 'priority'})
        response = self.client.get(reverse('cutting:planner'))
        self.assertContains(response, 'value="priority" form="planning-form" checked')

    def test_priority_radio_present_with_manual_priority_hint(self):
        response = self.client.get(reverse('cutting:planner'))
        self.assertContains(response, 'اولویت دستی موجودی')

    def test_preview_cnc_excludes_offcuts_by_default(self):
        self.add_stock(width='500.0', height='400.0', offcut=True)  # no full sheet at all
        data = self.client.get(reverse('cutting:planner_preview'), {
            'order_ids': [self.order.pk], 'planning_mode': 'cnc_sheets',
        }).json()
        row = data['materials'][0]
        self.assertEqual(row['piece_count'], 1)  # only the CNC-first piece counts here
        self.assertEqual(row['available_offcut_sheets'], 0)
        self.assertEqual(row['available_full_sheets'], 0)
        self.assertFalse(row['fits'])

    def test_preview_cnc_includes_offcuts_when_flagged(self):
        self.add_stock(width='500.0', height='400.0', offcut=True)
        data = self.client.get(reverse('cutting:planner_preview'), {
            'order_ids': [self.order.pk], 'planning_mode': 'cnc_sheets', 'cnc_use_offcuts': '1',
        }).json()
        row = data['materials'][0]
        self.assertEqual(row['available_offcut_sheets'], 1)
        self.assertTrue(row['fits'])

    def test_preview_standard_mode_always_includes_offcuts(self):
        self.add_stock(width='500.0', height='400.0', offcut=True)
        data = self.client.get(reverse('cutting:planner_preview'), {
            'order_ids': [self.order.pk], 'stock_policy': 'offcuts_first',
        }).json()
        row = data['materials'][0]
        self.assertEqual(row['available_offcut_sheets'], 1)
        self.assertTrue(row['fits'])


class PlannerPostCncOffcutTests(TestCase):
    """Full POST round-trip: the flag reserves the offcut and the operator is told."""

    def setUp(self):
        self.user = User.objects.create_superuser('offcut-post', 'up@example.test', 'pw')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='کارگاه پرت POST')
        self.material = Material.objects.create(name='MDF پرت POST', thickness=Decimal('16.0'))
        self.part = Part.objects.create(
            material=self.material, name='قطعه کوچک', length=Decimal('200.0'), width=Decimal('90.0'),
            pname='کمد', routing_code='cnc.paint', turn=False, f3='small.post.200x90',
        )
        self.order = Order.objects.create(customer=self.customer, status='planned')
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

    def post_plan(self, **extra):
        data = {'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0', 'planning_mode': 'cnc_sheets'}
        data.update(extra)
        return self.client.post(reverse('cutting:planner'), data, follow=True)

    def test_post_with_flag_uses_offcut_and_reports_it(self):
        response = self.post_plan(cnc_use_offcuts='1')
        plan = CncSheetPlan.objects.latest('pk')
        self.assertTrue(plan.source_stock.is_offcut)
        messages = [str(m) for m in response.context['messages']]
        self.assertTrue(any('روی پرت' in m for m in messages), messages)

    def test_post_without_flag_uses_full_sheet_and_no_offcut_message(self):
        response = self.post_plan()
        plan = CncSheetPlan.objects.latest('pk')
        self.assertFalse(plan.source_stock.is_offcut)
        messages = [str(m) for m in response.context['messages']]
        self.assertFalse(any('روی پرت' in m for m in messages), messages)


class InventoryOffcutLibraryTests(TestCase):
    """The offcut library page: lineage column and the 30-day created/consumed KPI."""

    def setUp(self):
        self.user = User.objects.create_superuser('offcut-lib', 'ul@example.test', 'pw')
        self.client.force_login(self.user)
        self.material = Material.objects.create(name='MDF پرت LIB', thickness=Decimal('16.0'))

    def add_offcut(self, **extra):
        defaults = dict(
            material=self.material, width=Decimal('400.0'), height=Decimal('300.0'), quantity=1,
            is_offcut=True, created_by=self.user,
        )
        defaults.update(extra)
        return SheetStock.objects.create(**defaults)

    def test_lineage_column_links_source_plan(self):
        plan = CutPlan.objects.create(status='cut', created_by=self.user)
        self.add_offcut(created_from_plan=plan, note='پرت واقعی برنامه')
        response = self.client.get(reverse('cutting:offcuts'))
        self.assertContains(response, f'برنامه #{plan.pk}')
        self.assertContains(response, reverse('cutting:plan_detail', args=[plan.pk]))

    def test_manual_offcut_shows_manual_origin_and_originated_one_shows_parent_stock(self):
        parent = SheetStock.objects.create(
            material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'), quantity=5,
            created_by=self.user,
        )
        self.add_offcut(origin=parent)
        self.add_offcut(note='ثبت دستی اپراتور')
        response = self.client.get(reverse('cutting:offcuts'))
        self.assertContains(response, 'ثبت دستی')
        self.assertContains(response, f'از ورق #{parent.pk}')

    def test_kpi_counts_last_30_days(self):
        offcut = self.add_offcut()
        from .models import SheetStockEvent
        SheetStockEvent.objects.create(
            stock=offcut, material=self.material, event_type='offcut', quantity_delta=1,
            width=offcut.width, height=offcut.height, created_by=self.user,
        )
        SheetStockEvent.objects.create(
            stock=offcut, material=self.material, event_type='consume', quantity_delta=-1,
            width=offcut.width, height=offcut.height, is_offcut=True, created_by=self.user,
        )
        response = self.client.get(reverse('cutting:offcuts'))
        self.assertEqual(response.context['offcut_created_30d'], 1)
        self.assertEqual(response.context['offcut_consumed_30d'], 1)
        self.assertContains(response, 'پرت — ۳۰ روز اخیر')

    def test_kpi_card_hidden_on_full_sheets_page(self):
        response = self.client.get(reverse('cutting:full_sheets'))
        self.assertNotContains(response, 'پرت — ۳۰ روز اخیر')
