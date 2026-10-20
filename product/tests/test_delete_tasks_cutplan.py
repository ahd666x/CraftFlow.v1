"""رفع ProtectedError هنگام بازگشت سفارش به پیش‌نویس وقتی تسک‌ها عضو برنامه‌ی برش‌اند."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from cutting.models import (
    CutPlan, CutPlanSheet, CutPlanSheetTask, CutPlanTask, SheetStock, SheetStockEvent,
)
from product.models import Customer, Order, OrderItem, ProductionTask, Product, ProductCategory


class DeleteTasksWithCutPlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('dtadmin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.category, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.admin, customer=cls.customer, number='DT-1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=1)
        cls.task_a = ProductionTask.objects.create(
            order=cls.order, order_item=cls.order_item,
            station_name='cut', step_order=1, quantity=1, status='pending',
        )
        cls.task_b = ProductionTask.objects.create(
            order=cls.order, order_item=cls.order_item,
            station_name='cnc', step_order=2, quantity=1, status='waiting',
        )

    def _make_plan(self, status='planned'):
        from product.models import Material
        material = Material.objects.create(name='ماده', thickness=Decimal('1'))
        stock = SheetStock.objects.create(
            material=material, width=Decimal('2400.0'), height=Decimal('1220.0'), quantity=1,
        )
        plan = CutPlan.objects.create(status=status)
        plan.orders.add(self.order)
        membership = CutPlanTask.objects.create(
            plan=plan, task=self.task_a, quantity=1, task_quantity=1,
        )
        CutPlanTask.objects.create(plan=plan, task=self.task_b, quantity=1, task_quantity=1)
        sheet = CutPlanSheet.objects.create(
            plan=plan, source_stock=stock, material=material,
            layout_index=0, width=Decimal('2400.0'), height=Decimal('1220.0'),
        )
        CutPlanSheetTask.objects.create(plan_sheet=sheet, task=self.task_a, quantity=1)
        CutPlanSheetTask.objects.create(plan_sheet=sheet, task=self.task_b, quantity=1)
        return plan, sheet

    def setUp(self):
        self.client.login(username='dtadmin', password='testpass')

    def test_delete_tasks_with_planned_plan_no_protected_error(self):
        """مسیر گزارش‌شده: POST /orders/<id>/delete-tasks/ نباید ProtectedError بدهد."""
        plan, sheet = self._make_plan(status='planned')
        response = self.client.post(reverse('delete_all_tasks', args=[self.order.id]))
        self.assertEqual(response.status_code, 302)

        self.assertFalse(ProductionTask.objects.filter(order=self.order).exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'draft')
        self.assertFalse(CutPlanTask.objects.exists())
        self.assertFalse(CutPlanSheetTask.objects.exists())

        plan.refresh_from_db()
        self.assertEqual(plan.status, 'cancelled')
        self.assertIsNotNone(plan.cancelled_at)
        sheet.refresh_from_db()
        self.assertEqual(sheet.outcome, 'released')
        self.assertTrue(SheetStockEvent.objects.filter(
            plan=plan, event_type='release').exists())

    def test_delete_tasks_with_cut_plan_keeps_plan_status(self):
        """برنامه‌ی نهایی‌شده ('cut') لغو نمی‌شود؛ فقط عضویت تسک‌های حذف‌شده پاک می‌شود."""
        plan, sheet = self._make_plan(status='cut')
        response = self.client.post(reverse('delete_all_tasks', args=[self.order.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProductionTask.objects.filter(order=self.order).exists())

        plan.refresh_from_db()
        self.assertEqual(plan.status, 'cut')
        self.assertIsNone(plan.cancelled_at)
        sheet.refresh_from_db()
        self.assertNotEqual(sheet.outcome, 'released')
        self.assertFalse(SheetStockEvent.objects.filter(plan=plan, event_type='release').exists())
        self.assertFalse(CutPlanTask.objects.exists())
        self.assertFalse(CutPlanSheetTask.objects.exists())

    def test_delete_tasks_without_cut_plan_unchanged(self):
        """بدون برنامه‌ی برش هم رفتار قبلی سالم می‌ماند (رگرسیون)."""
        response = self.client.post(reverse('delete_all_tasks', args=[self.order.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProductionTask.objects.filter(order=self.order).exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'draft')
        self.assertFalse(CutPlan.objects.exists())
