"""
تست تحلیل تولید (فاز ۲) — گلوگاه ایستگاه و سطل ایستگاه ناشناس.

تمرکز اصلی این فایل روی سه چیز است:
  1. «تعداد سفارش‌های درگیر» با query گروه‌بندی‌شده محاسبه شود، نه N+1.
  2. ایستگاه‌های نامعتبر **حذف نشوند** و به‌صورت سطل مستقل دیده شوند.
  3. ظرفیت عددی ایستگاه چون در CraftFlow ذخیره نشده، گزارش نشود.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from craftflow_ai.analysis.limits import unavailable
from craftflow_ai.analysis.production import (
    STATION_CODES,
    STATION_LABELS,
    derived_blocked_filter,
    derived_in_progress_filter,
    station_bottleneck,
    unknown_station_bucket,
)
from product.models import ProductionTask

from .factories import make_order, make_tasks, make_user

VALID_STAGES = tuple(code for code, _ in STATION_LABELS.items())


class StationRegistryTests(TestCase):
    def test_station_codes_match_the_real_model(self):
        self.assertEqual(STATION_CODES, VALID_STAGES)
        self.assertIn('paint', STATION_CODES)
        self.assertIn('vacum', STATION_CODES)
        self.assertNotIn('assembly1', STATION_CODES)

    def test_every_station_can_be_analysed(self):
        for code in STATION_CODES:
            report = station_bottleneck(code)
            self.assertEqual(report['stage'], code)
            for key in ('pending', 'waiting', 'done', 'in_progress', 'blocked',
                        'open_tasks', 'oldest_pending_age_days', 'affected_orders',
                        'workers', 'unknown_station', 'confidence', 'limitations',
                        'evidence'):
                self.assertIn(key, report, code)


class StationBottleneckCountingTests(TestCase):
    def setUp(self):
        self.order_a = make_order(status='producing')
        self.order_b = make_order(status='producing')
        self.tasks_a = make_tasks(self.order_a, stations=('paint', 'cut'),
                                  statuses=('pending', 'done'))
        self.tasks_b = make_tasks(self.order_b, stations=('paint',),
                                  statuses=('waiting',))

    def test_counts_and_derived_states(self):
        report = station_bottleneck('paint')
        self.assertEqual(report['pending'], 1)
        self.assertEqual(report['waiting'], 1)
        self.assertEqual(report['open_tasks'], 2)

    def test_affected_orders_counts_distinct_orders(self):
        report = station_bottleneck('paint')
        self.assertEqual(report['affected_orders'], 2)
        self.assertCountEqual(
            report['affected_order_ids'],
            [self.order_a.id, self.order_b.id],
        )

    def test_affected_orders_is_not_task_count(self):
        """دو تسک از یک سفارش نباید دو سفارش شمرده شود."""
        # از ``update`` استفاده می‌شود نه ``save``: ``ProductionTask.save()``
        # در CraftFlow اثر جانبی دارد (تبدیل waiting به pending و مصرف ماده).
        ProductionTask.objects.filter(pk=self.tasks_b[0].pk).update(station_name='cut')
        report = station_bottleneck('paint')
        self.assertEqual(report['open_tasks'], 1)
        self.assertEqual(report['affected_orders'], 1)

    def test_affected_orders_is_bounded(self):
        ids = station_bottleneck('paint')['affected_order_ids']
        self.assertIsInstance(ids, list)
        self.assertLessEqual(len(ids), 50)

    def test_completed_station_is_complete(self):
        make_tasks(self.order_a, stations=('dr',), statuses=('done',))
        report = station_bottleneck('dr')
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['open_tasks'], 0)
        self.assertEqual(report['affected_orders'], 0)

    def test_station_without_any_task_is_not_started(self):
        report = station_bottleneck('shipping')
        self.assertEqual(report['status'], 'not_started')
        self.assertEqual(report['confidence'], 'low')
        self.assertTrue(report['limitations'])

    def test_in_progress_is_derived_and_documented(self):
        worker = make_user('worker_a', groups=['1'])
        past = timezone.now() - timedelta(hours=3)
        ProductionTask.objects.filter(pk=self.tasks_a[0].pk).update(
            assigned_worker=worker, scheduled_start=past,
        )
        report = station_bottleneck('paint')
        self.assertEqual(report['in_progress'], 1)
        self.assertIn('in_progress', report['derived_fields'])
        self.assertIn('ProductionTask', report['derived_fields']['note'])

    def test_blocked_is_derived_from_schedule_overrun(self):
        past = timezone.now() - timedelta(days=1)
        ProductionTask.objects.filter(pk=self.tasks_a[0].pk).update(
            scheduled_start=past - timedelta(hours=2), scheduled_end=past,
        )
        report = station_bottleneck('paint')
        self.assertEqual(report['blocked'], 1)
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('scheduled_end', report['derived_fields']['blocked'])

    def test_derived_filters_are_reusable_predicates(self):
        now = timezone.now()
        self.assertIn('status', str(derived_in_progress_filter(now)))
        self.assertIn('scheduled_end', str(derived_blocked_filter(now)))


class StationWorkersTests(TestCase):
    def test_assigned_workers_are_listed(self):
        order = make_order(status='producing')
        tasks = make_tasks(order, stations=('paint', 'prs'), statuses=('pending', 'pending'))
        worker = make_user('painter_one', groups=['1'])
        ProductionTask.objects.filter(pk=tasks[0].pk).update(assigned_worker=worker)

        report = station_bottleneck('paint')
        self.assertEqual(report['workers_count'], 1)
        self.assertEqual(report['workers'][0]['worker_id'], worker.id)
        self.assertEqual(report['workers'][0]['assigned_task_count'], 1)

    def test_station_without_workers_reports_empty_list(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('paint',), statuses=('pending',))
        report = station_bottleneck('paint')
        self.assertEqual(report['workers'], [])


class OldestPendingAgeTests(TestCase):
    def test_oldest_pending_age_is_derived_from_order_creation(self):
        import jdatetime

        old = jdatetime.date.today() - jdatetime.timedelta(days=12)
        order = make_order(status='producing', created_at=old)
        make_tasks(order, stations=('paint',), statuses=('pending',))

        report = station_bottleneck('paint')
        self.assertEqual(report['oldest_pending_age_days'], 12)
        self.assertEqual(report['oldest_pending_order_id'], order.id)
        self.assertIn('missing_task_timestamp', report['limitations'])

    def test_no_open_tasks_means_no_age(self):
        make_order(status='producing')
        report = station_bottleneck('paint')
        self.assertIsNone(report['oldest_pending_age_days'])


class UnknownStationBucketTests(TestCase):
    def test_empty_when_every_station_is_valid(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut', 'cnc'))
        bucket = unknown_station_bucket()
        self.assertEqual(bucket['status'], 'empty')
        self.assertEqual(bucket['task_count'], 0)
        self.assertEqual(bucket['open_task_count'], 0)

    def test_dirty_stations_are_never_dropped(self):
        order = make_order(status='producing')
        dirty = ('frez', '2set', 'cnc,mon', '300x120', 'assembly1')
        for index, station in enumerate(dirty, start=1):
            ProductionTask.objects.create(
                order=order, station_name=station, step_order=index,
                quantity=1, status='pending',
            )

        bucket = unknown_station_bucket()
        self.assertEqual(bucket['status'], 'present')
        self.assertEqual(bucket['task_count'], len(dirty))
        self.assertEqual(bucket['open_task_count'], len(dirty))
        self.assertEqual(bucket['affected_order_count'], 1)
        self.assertEqual(bucket['station_names_total'], len(set(dirty)))

    def test_dirty_names_are_not_mapped_to_known_stations(self):
        order = make_order(status='producing')
        ProductionTask.objects.create(
            order=order, station_name='cnc,mon', step_order=1,
            quantity=1, status='pending',
        )
        bucket = unknown_station_bucket()
        self.assertIn('cnc,mon', bucket['station_names_observed'])
        self.assertNotIn('cnc', bucket['station_names_observed'])
        for row in bucket['rows']:
            self.assertFalse(row['known_station'])

    def test_bucket_appears_in_every_station_analysis(self):
        order = make_order(status='producing')
        ProductionTask.objects.create(
            order=order, station_name='frez', step_order=1,
            quantity=1, status='pending',
        )
        for code in ('cut', 'paint', 'shipping'):
            report = station_bottleneck(code)
            self.assertIn('unknown_station', report)
            self.assertEqual(report['unknown_station']['open_task_count'], 1)
            self.assertIn('unknown_station_codes', report['limitations'])

    def test_bucket_rows_are_sorted_by_open_tasks(self):
        order = make_order(status='producing')
        for index in range(3):
            ProductionTask.objects.create(
                order=order, station_name='frez', step_order=index + 1,
                quantity=1, status='pending',
            )
        ProductionTask.objects.create(
            order=order, station_name='2set', step_order=9,
            quantity=1, status='pending',
        )
        bucket = unknown_station_bucket()
        self.assertEqual(bucket['rows'][0]['station_name'], 'frez')
        self.assertEqual(bucket['rows'][0]['open_tasks'], 3)

    def test_bucket_exposes_evidence_with_source_and_query(self):
        bucket = unknown_station_bucket()
        for key in ('metric', 'value', 'unit', 'source', 'query'):
            self.assertIn(key, bucket['evidence'])
        self.assertNotIn('SELECT', bucket['evidence']['query'].upper())


class StationCapacityNotComputableTests(TestCase):
    def test_capacity_is_explicitly_unavailable(self):
        make_order(status='producing')
        report = station_bottleneck('paint')
        capacity = report['capacity']
        self.assertEqual(capacity['status'], 'insufficient_data')
        self.assertEqual(capacity['reason'], 'missing_station_capacity')
        self.assertIn('station_capacity_simulation',
                      capacity['blocked_capabilities'])

    def test_capacity_is_never_reported_as_zero_or_a_number(self):
        make_order(status='producing')
        report = station_bottleneck('paint')
        self.assertNotIsInstance(report['capacity'], (int, float))

    def test_no_hardcoded_workday_assumption_exists(self):
        """فرض‌هایی مثل ۸ ساعت کاری در روز نباید در تحلیل باشند."""
        from pathlib import Path

        source = Path('craftflow_ai/analysis/production.py').read_text(encoding='utf-8')
        for token in ('8 * 60', '480', 'workday_hours', 'hours_per_day'):
            self.assertNotIn(token, source, token)

    def test_missing_station_capacity_limit_exists(self):
        payload = unavailable('missing_station_capacity').to_dict()
        self.assertEqual(payload['reason'], 'missing_station_capacity')


class StationFindingsTests(TestCase):
    def test_findings_carry_evidence_and_confidence(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('paint',), statuses=('waiting',))
        report = station_bottleneck('paint')
        self.assertTrue(report['findings'])
        for finding in report['findings']:
            self.assertTrue(finding['finding_id'])
            self.assertEqual(finding['domain'], 'production')
            self.assertTrue(finding['evidence'])
            self.assertIn(finding['confidence'], ('high', 'medium', 'low'))
            if finding.get('derived'):
                self.assertTrue(finding['limitations'], finding['finding_id'])

    def test_shared_queue_across_orders_is_a_finding(self):
        order_a = make_order(status='producing')
        order_b = make_order(status='producing')
        make_tasks(order_a, stations=('paint',), statuses=('pending',))
        make_tasks(order_b, stations=('paint',), statuses=('pending',))

        report = station_bottleneck('paint')
        ids = {f['finding_id'] for f in report['findings']}
        self.assertIn('paint:queue', ids)

    def test_solitary_order_queue_finding_is_informational_only(self):
        """یک سفارش تنها یعنی صف تک‌سفارشی؛ یافته فقط جنبهٔ اطلاع‌رسانی دارد."""
        order = make_order(status='producing')
        make_tasks(order, stations=('paint',), statuses=('pending',))
        report = station_bottleneck('paint')
        queue = [f for f in report['findings'] if f['finding_id'] == 'paint:queue']
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['severity'], 'info')


class InvalidInputTests(TestCase):
    def test_unknown_station_returns_none(self):
        for value in ('frez', '2set', 'assembly1', '', None, 'CUT'):
            self.assertIsNone(station_bottleneck(value), value)

    def test_station_code_case_is_respected(self):
        """کد ایستگاه در CraftFlow lowercase است و نباید حدس زده شود."""
        self.assertIsNone(station_bottleneck('PAINT'))
        self.assertIsNone(station_bottleneck('Cut'))