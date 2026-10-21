# -*- coding: utf-8 -*-
"""Batch-A hardening tests for the cutting side:
- mark_cut no longer defaults a missing outcome to "used";
- expected_offcuts forecasts respect the independent trim on the height edge.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CutPlan, SheetStock
from .optimizer import expected_offcuts


class MarkCutOutcomeMandatoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='cut-admin-h', email='cut-h@example.test', password='test-password')
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name='کارگاه آزمون سخت‌سازی')
        self.material = Material.objects.create(name='MDF سخت‌سازی', thickness=Decimal('16.0'))
        self.part = Part.objects.create(material=self.material, name='دیواره', length=Decimal('400.0'), width=Decimal('300.0'), pname='کمد', routing_code='cut', turn=True)
        self.order = Order.objects.create(customer=self.customer, status='planned')
        self.task = ProductionTask.objects.create(order=self.order, part=self.part, station_name='cut', step_order=1, quantity=1, status='pending')
        SheetStock.objects.create(material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'), quantity=1, is_offcut=False, priority=100, created_by=self.user)
        response = self.client.post(reverse('cutting:planner'), {'action': 'plan', 'order_ids': [self.order.pk], 'kerf': '4.0'})
        self.assertEqual(response.status_code, 302)
        self.plan = CutPlan.objects.latest('pk')

    def test_missing_outcome_no_longer_defaults_to_used(self):
        sheet = self.plan.planned_sheets.get()
        # POST دست‌ساز بدون هیچ فیلدی — قبلاً به‌صورت خاموش «used» ثبت می‌شد.
        response = self.client.post(reverse('cutting:mark_cut', args=[self.plan.pk]), {})
        self.plan.refresh_from_db()
        sheet.refresh_from_db()
        self.assertEqual(self.plan.status, 'planned')
        self.assertNotEqual(sheet.outcome, 'used')
        # تسک هم نباید کامل شده باشد.
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_quantity, 0)

    def test_explicit_outcome_still_works(self):
        sheet = self.plan.planned_sheets.get()
        response = self.client.post(reverse('cutting:mark_cut', args=[self.plan.pk]), {f'outcome_{sheet.pk}': 'used'})
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, 'cut')


class ExpectedOffcutsTrimTests(TestCase):
    def _layout(self):
        return {
            'sheet': {'width': Decimal('1000'), 'height': Decimal('500'), 'is_offcut': False},
            'parts': [{'width': Decimal('400'), 'height': Decimal('200'), 'x': Decimal('0'), 'y': Decimal('0'), 'row': 0}],
        }

    def test_trim_shrinks_only_the_bottom_remnant(self):
        layout = self._layout()
        without_trim = expected_offcuts(layout, kerf=4)
        with_trim = expected_offcuts(layout, kerf=4, trim=Decimal('10'))
        bottom_no = next(r for r in without_trim if r['y'] == Decimal('204'))
        bottom_with = next(r for r in with_trim if r['y'] == Decimal('204'))
        self.assertEqual(bottom_with['height'], bottom_no['height'] - Decimal('10'))

    def test_side_remnants_are_unaffected_by_trim(self):
        layout = self._layout()
        without_trim = expected_offcuts(layout, kerf=4)
        with_trim = expected_offcuts(layout, kerf=4, trim=Decimal('10'))
        side_no = next(r for r in without_trim if r['x'] == Decimal('404'))
        side_with = next(r for r in with_trim if r['x'] == Decimal('404'))
        self.assertEqual(side_no['height'], side_with['height'])

    def test_huge_trim_removes_the_bottom_remnant_forecast(self):
        layout = self._layout()
        with_trim = expected_offcuts(layout, kerf=4, trim=Decimal('400'))
        self.assertFalse(any(r['y'] == Decimal('204') for r in with_trim))
