"""تست واحد ابزارها — هر ابزار، حالت خالی، حالت پرداده، و حالت خطا."""
from decimal import Decimal

from django.test import TestCase

from craftflow_ai.tools import get_registry
from craftflow_ai.tools import inventory, orders, planning, production, reports

from .factories import (
    make_leftover,
    make_open_issue,
    make_order,
    make_raw_material,
    make_tasks,
)


class ToolTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.registry = get_registry()

    def run_tool(self, module_func, **kwargs):
        return module_func(**kwargs)

    def assertSuccess(self, result):
        self.assertTrue(result['success'], msg=result)
        self.assertIn('data', result)
        self.assertIn('generated_at', result['metadata'])
        self.assertEqual(result['metadata']['source'], 'craftflow')
        return result['data']


class EmptyDatabaseTests(ToolTestBase):
    """دیتابیس خالی: ابزار نباید خطا بدهد و نباید چیزی اختراع کند."""

    def test_get_open_orders_on_empty_database(self):
        data = self.assertSuccess(orders.get_open_orders())
        self.assertEqual(data['orders'], [])
        self.assertEqual(data['total_open_orders'], 0)

    def test_get_production_status_on_empty_database(self):
        data = self.assertSuccess(production.get_production_status())
        self.assertEqual(data['tasks']['pending'], 0)
        self.assertIsNone(data['busiest_stage'])
        self.assertEqual(len(data['stations']), 11)  # تعداد واقعی STATION_CHOICES

    def test_find_production_bottlenecks_on_empty_database(self):
        data = self.assertSuccess(production.find_production_bottlenecks())
        self.assertEqual(data['bottlenecks'], [])

    def test_get_inventory_status_on_empty_database(self):
        data = self.assertSuccess(inventory.get_inventory_status())
        self.assertEqual(data['total_materials'], 0)
        self.assertEqual(data['low_stock_count'], 0)

    def test_find_material_shortages_on_empty_database(self):
        data = self.assertSuccess(inventory.find_material_shortages())
        self.assertEqual(data['shortages'], [])
        self.assertEqual(data['shortage_count'], 0)

    def test_generate_production_report_on_empty_database(self):
        data = self.assertSuccess(reports.generate_production_report())
        self.assertEqual(data['production']['orders']['total'], 0)


class OrderToolTests(ToolTestBase):

    def test_get_open_orders(self):
        first = make_order(status='producing', customer_name='مشتری الف')
        second = make_order(status='completed', customer_name='مشتری ب')
        make_tasks(first, stations=('cut', 'cnc'))
        first.tasks.update(status='done')

        data = self.assertSuccess(orders.get_open_orders())

        ids = [row['order_id'] for row in data['orders']]
        self.assertIn(first.id, ids)
        self.assertNotIn(second.id, ids, 'سفارش تکمیل‌شده نباید «باز» باشد')
        row = next(r for r in data['orders'] if r['order_id'] == first.id)
        self.assertEqual(row['tasks_total'], 2)
        self.assertEqual(row['tasks_done'], 2)
        self.assertEqual(row['progress_percent'], 100)

    def test_get_order_details(self):
        order = make_order()
        make_tasks(order, stations=('cut', 'cnc'), statuses=['done', 'pending'])

        data = self.assertSuccess(orders.get_order_details(order.id))

        self.assertEqual(data['order_id'], order.id)
        self.assertEqual(data['items_count'], 1)
        self.assertEqual(data['tasks_total'], 2)
        stages = {s['stage']: s for s in data['stages']}
        self.assertEqual(stages['cut']['done'], 1)
        self.assertEqual(stages['cnc']['pending'], 1)

    def test_get_order_details_invalid_order_id(self):
        result = orders.get_order_details(999999)
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['code'], 'ORDER_NOT_FOUND')

    def test_get_order_details_non_numeric_order_id(self):
        result = orders.get_order_details('abc')
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['code'], 'ORDER_NOT_FOUND')

    def test_find_delayed_orders_uses_existing_rule(self):
        import jdatetime

        old = make_order(customer_name='قدیمی')
        old.created_at = jdatetime.date.today() - jdatetime.timedelta(days=10)
        old.save()
        make_tasks(old)

        recent = make_order(customer_name='جدید')
        make_tasks(recent)

        data = self.assertSuccess(orders.find_delayed_orders())

        ids = [row['order_id'] for row in data['delayed_orders']]
        self.assertIn(old.id, ids)
        self.assertNotIn(recent.id, ids)
        self.assertEqual(data['rule']['excludes_status'], 'completed')

    def test_find_delayed_orders_excludes_completed(self):
        import jdatetime

        done = make_order(status='completed', customer_name='تمام‌شدهٔ قدیمی')
        done.created_at = jdatetime.date.today() - jdatetime.timedelta(days=30)
        done.save()

        data = self.assertSuccess(orders.find_delayed_orders())
        self.assertNotIn(
            done.id, [row['order_id'] for row in data['delayed_orders']]
        )


class ProductionToolTests(ToolTestBase):

    def test_get_production_status_counts_stations(self):
        order = make_order()
        make_tasks(order, stations=('cut', 'cnc', 'mon'))

        data = self.assertSuccess(production.get_production_status())

        stages = {s['stage']: s for s in data['stations']}
        self.assertEqual(stages['cut']['pending'], 1)
        self.assertEqual(stages['cnc']['waiting'], 1)
        self.assertEqual(stages['mon']['waiting'], 1)
        self.assertEqual(data['tasks']['pending'], 1)
        self.assertEqual(data['tasks']['waiting'], 2)

    def test_production_status_uses_real_station_names(self):
        from product.models import STATION_CHOICES

        data = self.assertSuccess(production.get_production_status())
        self.assertEqual(
            [s['stage'] for s in data['stations']],
            [code for code, _ in STATION_CHOICES],
        )

    def test_get_pending_tasks(self):
        order = make_order()
        make_tasks(order, stations=('cut', 'cnc'))

        data = self.assertSuccess(production.get_pending_tasks())

        self.assertEqual(data['total'], 1)
        task = data['tasks'][0]
        self.assertEqual(task['order_id'], order.id)
        self.assertEqual(task['stage'], 'cut')
        self.assertEqual(task['stage_label'], 'برش')

    def test_get_pending_tasks_filtered_by_stage(self):
        order = make_order()
        make_tasks(order, stations=('cut', 'cnc'))
        order.tasks.update(status='pending')

        data = self.assertSuccess(production.get_pending_tasks(stage='cnc'))
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['tasks'][0]['stage'], 'cnc')

    def test_get_pending_tasks_unknown_stage_returns_error(self):
        result = production.get_pending_tasks(stage='nonexistent_stage')
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['code'], 'UNKNOWN_STAGE')

    def test_find_production_bottlenecks(self):
        order = make_order()
        make_tasks(order, stations=('cut', 'cnc', 'mon'))

        data = self.assertSuccess(production.find_production_bottlenecks())

        necks = data['bottlenecks']
        self.assertTrue(necks)
        by_stage = {n['stage']: n for n in necks}
        self.assertIn('cut', by_stage)
        self.assertEqual(by_stage['cut']['pending_tasks'], 1)
        self.assertEqual(by_stage['cnc']['waiting_tasks'], 1)
        self.assertIsNotNone(by_stage['cut']['oldest_pending'])
        self.assertIn('تسک', by_stage['cut']['reason'])

    def test_bottlenecks_sorted_by_queue_size(self):
        from product.models import ProductionTask

        order = make_order()
        make_tasks(order, stations=('cut', 'cnc', 'mon'))
        # مونتاژ را پر کن تا بزرگ‌ترین صف شود
        for offset in (100, 101):
            ProductionTask.objects.create(
                order=order, station_name='mon', step_order=offset,
                quantity=1, status='pending',
            )

        data = self.assertSuccess(production.find_production_bottlenecks(limit=1))
        self.assertEqual(data['bottlenecks'][0]['stage'], 'mon')
        # مونتاژ: ۱ تسک در انتظار + ۲ تسک آماده = ۳ تسک باز
        self.assertEqual(data['bottlenecks'][0]['open_tasks'], 3)
        self.assertEqual(data['bottlenecks'][0]['pending_tasks'], 2)


class InventoryToolTests(ToolTestBase):

    def test_get_inventory_status_detects_low_stock(self):
        make_raw_material(name='رنگ سفید', stock=Decimal('2'), min_alert=Decimal('5'))
        make_raw_material(name='رنگ مشکی', stock=Decimal('50'), min_alert=Decimal('5'))

        data = self.assertSuccess(inventory.get_inventory_status())

        self.assertEqual(data['total_materials'], 2)
        self.assertEqual(data['low_stock_count'], 1)
        self.assertEqual(data['low_stock_materials'][0]['name'], 'رنگ سفید')
        self.assertEqual(data['low_stock_materials'][0]['status'], 'low')

    def test_inventory_status_detects_out_of_stock(self):
        make_raw_material(name='خالی', stock=Decimal('0'), min_alert=Decimal('1'))

        data = self.assertSuccess(inventory.get_inventory_status())
        self.assertEqual(data['out_of_stock_count'], 1)
        self.assertEqual(data['out_of_stock_materials'][0]['status'], 'out_of_stock')

    def test_inventory_stock_reflects_consumption_movement(self):
        raw = make_raw_material(name='رنگ', stock=Decimal('10'))
        from inventory.models import StockMovement
        StockMovement.objects.create(
            raw_material=raw, movement_type='consumption', quantity=Decimal('3'),
        )

        data = self.assertSuccess(inventory.get_inventory_status())
        row = next(m for m in data['materials'] if m['name'] == 'رنگ')
        self.assertEqual(row['stock'], '7.00')

    def test_find_material_shortages_uses_build_plans_math(self):
        """کمبود باید با گِرد کردن به بسته محاسبه شود، نه ساده."""
        order = make_order()
        make_tasks(order, stations=('cut',))
        raw = make_raw_material(name='رنگ کم', stock=Decimal('2'), min_alert=Decimal('0'))
        make_open_issue(raw, order, quantity=Decimal('6'))

        data = self.assertSuccess(inventory.find_material_shortages())

        self.assertEqual(data['shortage_count'], 1)
        row = data['shortages'][0]
        # نیاز ۶ لیتر با بستهٔ ۴ لیتری = ۸ لیتر فیزیکی
        self.assertEqual(row['physical_required'], '8.00')
        self.assertEqual(row['current_stock'], '2.00')
        self.assertEqual(row['shortage_amount'], '6.00')
        self.assertEqual(row['packs'], 2)

    def test_find_material_shortages_uses_floor_leftover(self):
        order = make_order()
        make_tasks(order, stations=('cut',))
        raw = make_raw_material(name='رنگ', stock=Decimal('20'), min_alert=Decimal('0'))
        make_leftover(raw, Decimal('4'))
        make_open_issue(raw, order, quantity=Decimal('6'))

        data = self.assertSuccess(inventory.find_material_shortages())
        # ۴ از باقی‌ماندهٔ سالن + ۲ از انبار؛ با بستهٔ ۴ لیتری یعنی ۴ لیتر فیزیکی
        self.assertEqual(data['shortage_count'], 0)

    def test_get_material_requirements(self):
        order = make_order()
        make_tasks(order, stations=('cut',))
        raw = make_raw_material(name='رنگ', stock=Decimal('10'))
        make_open_issue(raw, order, quantity=Decimal('8'))

        data = self.assertSuccess(inventory.get_material_requirements(order.id))

        self.assertEqual(data['order_id'], order.id)
        self.assertEqual(data['open_issue_count'], 1)
        issue = data['material_issues'][0]
        self.assertEqual(issue['raw_material'], 'رنگ')
        self.assertEqual(issue['remaining_quantity'], '8.00')

    def test_get_material_requirements_invalid_order(self):
        result = inventory.get_material_requirements(999999)
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['code'], 'ORDER_NOT_FOUND')

    def test_get_material_requirements_without_order_id(self):
        result = inventory.get_material_requirements(None)
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['code'], 'MISSING_ARGUMENT')


class ReportAndPlanningToolTests(ToolTestBase):

    def test_generate_production_report(self):
        order = make_order()
        make_tasks(order, stations=('cut', 'cnc'))

        data = self.assertSuccess(reports.generate_production_report())

        self.assertEqual(data['production']['orders']['total'], 1)
        self.assertTrue(data['sources'])
        self.assertIn('production.production_status', data['sources'])

    def test_get_order_timeline(self):
        order = make_order()
        make_tasks(order, stations=('cut', 'cnc'), statuses=['done', 'pending'])

        data = self.assertSuccess(planning.get_order_timeline(order.id))

        self.assertEqual(data['total_steps'], 2)
        self.assertEqual(data['completed_steps'], 1)
        self.assertIsNotNone(data['current_step'])

    def test_get_order_timeline_invalid_order(self):
        result = planning.get_order_timeline(999999)
        self.assertFalse(result['success'])
        self.assertEqual(result['error']['code'], 'ORDER_NOT_FOUND')

    def test_get_working_day_info(self):
        data = self.assertSuccess(planning.get_working_day_info())
        self.assertIn('is_working_day', data)
        self.assertTrue(data['today_jalali'])