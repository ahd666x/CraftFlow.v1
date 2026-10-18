"""
Tests for the "each queue row may be returned at most once" rule.

These tests live in their own module so the old behaviour (multiple returns
per row) is never silently re-enabled by a refactor.
"""
from datetime import date

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from inventory.models import DailyMaterialClosing, DailyMaterialQueue, StockMovement
from inventory.services import (
    daily_queue_action_state,
    execute_daily_delivery,
    execute_daily_return,
    HandoverError,
)


def _make_material():
    from inventory.models import RawMaterial, RawMaterialCategory
    category, _ = RawMaterialCategory.objects.get_or_create(
        name='تست', defaults={'name': 'تست'},
    )
    material, _ = RawMaterial.objects.get_or_create(
        code='RET-ONCE-001',
        defaults={
            'name': 'مادهٔ بازگشت یک‌باره',
            'category': category,
            'unit': 'kg',
            'pack_size': 1,
            'min_stock_alert': 0,
            'is_active': True,
        },
    )
    return material


def _warehouse_user(username):
    user = User.objects.create_user(username=username, password='x', is_staff=True)
    group, _ = Group.objects.get_or_create(name='انبار')
    user.groups.add(group)
    return user


class ReturnOnceServiceTests(TestCase):
    """Service-level: a second return is refused."""

    @classmethod
    def setUpTestData(cls):
        cls.warehouse = _warehouse_user('warehouse')
        cls.worker = User.objects.create_user(
            username='worker', password='x', is_staff=True,
        )
        cls.material = _make_material()

    def _make_queue(self, delivered=10, returned=0, planned=10):
        queue = DailyMaterialQueue.objects.create(
            work_date=date(2026, 1, 5),
            worker=self.worker,
            raw_material=self.material,
            painting_stage=None,
            color_part='',
            planned_quantity=planned,
            delivered_quantity=delivered,
            returned_quantity=returned,
            status='delivered',
        )
        return queue

    def test_first_return_succeeds(self):
        queue = self._make_queue()
        execute_daily_return(
            queue_id=queue.pk,
            returned_by=self.warehouse,
            returned_quantity='4',
        )
        queue.refresh_from_db()
        self.assertEqual(float(queue.returned_quantity), 4.0)
        self.assertEqual(StockMovement.objects.filter(
            daily_queue=queue, movement_type='return',
        ).count(), 1)

    def test_second_return_raises_handover_error(self):
        queue = self._make_queue()
        execute_daily_return(
            queue_id=queue.pk,
            returned_by=self.warehouse,
            returned_quantity='4',
        )
        with self.assertRaises(HandoverError) as cm:
            execute_daily_return(
                queue_id=queue.pk,
                returned_by=self.warehouse,
                returned_quantity='2',
            )
        self.assertIn('قبلاً ثبت شده', str(cm.exception))
        # No second StockMovement should have been created.
        self.assertEqual(StockMovement.objects.filter(
            daily_queue=queue, movement_type='return',
        ).count(), 1)

    def test_can_return_false_after_return(self):
        queue = self._make_queue()
        execute_daily_return(
            queue_id=queue.pk,
            returned_by=self.warehouse,
            returned_quantity='4',
        )
        queue.refresh_from_db()
        state = daily_queue_action_state(queue)
        self.assertFalse(state['can_return'])
        self.assertTrue(state['return_recorded'])

    def test_locked_day_disables_all_actions(self):
        queue = self._make_queue()
        DailyMaterialClosing.objects.create(
            work_date=queue.work_date,
            status='locked',
            closed_by=self.warehouse,
            locked_by=self.warehouse,
        )
        state = daily_queue_action_state(queue)
        self.assertTrue(state['locked'])
        self.assertFalse(state['can_deliver'])
        self.assertFalse(state['can_return'])
        self.assertFalse(state['can_add_extra'])
        self.assertFalse(state['can_auto_return'])
        self.assertFalse(state['can_cancel'])


class ReturnOnceTemplateTests(TestCase):
    """Template-level: the return button disappears after a return."""

    @classmethod
    def setUpTestData(cls):
        cls.warehouse = _warehouse_user('warehouse2')
        cls.worker = User.objects.create_user(
            username='worker2', password='x', is_staff=True,
        )
        cls.material = _make_material()

    def test_button_absent_after_return(self):
        from django.test import Client
        queue = DailyMaterialQueue.objects.create(
            work_date=date(2026, 1, 6),
            worker=self.worker,
            raw_material=self.material,
            painting_stage=None,
            color_part='',
            planned_quantity=10,
            delivered_quantity=10,
            returned_quantity=0,
            status='delivered',
        )
        # Force the row to be returned.
        execute_daily_return(
            queue_id=queue.pk,
            returned_by=self.warehouse,
            returned_quantity='10',
        )
        queue.refresh_from_db()

        c = Client()
        c.force_login(self.warehouse)
        url = reverse('inventory:daily_material_queue') + f'?date=1404-10-16'
        response = c.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # The button class must not appear as an actual button element for a
        # returned row.  (The string also appears in the JS handler, so we
        # check for the rendered button markup, not the substring.)
        self.assertNotIn('btn btn-outline-warning btn-return', content)