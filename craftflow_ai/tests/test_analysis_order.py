"""
تست تحلیل سفارش (فاز ۲) — سلامت شش‌دامنه‌ای و علت تأخیر.

این تست‌ها روی دادهٔ واقعی CraftFlow ساخته می‌شوند و هم‌زمان شکاف‌های
واقعی دادهٔ کارخانه را قفل می‌کنند:

* ``Order.due_date`` خالی → اطمینان تأخیر باید ``low`` باشد.
* ``Material.raw_material`` خالی → دامنهٔ مواد باید ``insufficient_data`` باشد
  و **هرگز** «بدون کمبود» گزارش نشود.
* ``ProductionDefect`` خالی → دامنهٔ کیفیت باید ``unknown`` باشد، نه ``healthy``.
* جدول Holiday خالی است و نباید به‌عنوان دادهٔ در دسترس گزارش شود.
"""
import jdatetime
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from craftflow_ai.analysis.order import (
    DELAY_CAUSE_PRECEDENCE,
    DELAY_CAUSE_SEVERITY,
    HEALTH_DOMAINS,
    order_delay,
    order_health,
    predecessor_key,
)
from product.models import (
    Material,
    Order,
    Part,
    ProductionDefect,
    ProductionTask,
)

from .factories import (
    make_defect,
    make_order,
    make_packaging_units,
    make_tasks,
)

OPEN = ('pending', 'waiting')


class OrderHealthShapeTests(TestCase):
    def setUp(self):
        self.order = make_order(status='producing')
        make_tasks(self.order, stations=('cut', 'cnc'), statuses=('done', 'pending'))

    def test_all_six_domains_are_always_present(self):
        report = order_health(self.order.id)
        self.assertEqual(tuple(report['domains'].keys()), HEALTH_DOMAINS)
        self.assertEqual(len(report['domains']), 6)

    def test_every_domain_has_an_explicit_status(self):
        report = order_health(self.order.id)
        for domain, payload in report['domains'].items():
            self.assertTrue(payload['status'], domain)
            self.assertIn(payload['status'], {
                'healthy', 'warning', 'stalled', 'blocked', 'in_progress',
                'complete', 'not_started', 'unknown', 'insufficient_data',
            }, domain)

    def test_every_domain_has_evidence_and_confidence(self):
        report = order_health(self.order.id)
        for domain, payload in report['domains'].items():
            self.assertTrue(payload['evidence'], domain)
            self.assertIn(payload['confidence'], ('high', 'medium', 'low'), domain)

    def test_invalid_order_id_returns_none(self):
        self.assertIsNone(order_health(999999))
        self.assertIsNone(order_health('not-a-number'))
        self.assertIsNone(order_health(None))

    def test_order_without_tasks_is_not_started(self):
        empty = make_order(status='draft')
        report = order_health(empty.id)
        self.assertEqual(report['domains']['production']['status'], 'not_started')

    def test_production_is_healthy_when_some_done_and_nothing_overdue(self):
        report = order_health(self.order.id)
        self.assertEqual(report['domains']['production']['status'], 'healthy')

    def test_production_is_complete_when_everything_done(self):
        make_tasks(self.order, stations=('prs',), statuses=('done',))
        ProductionTask.objects.filter(order=self.order, station_name='cnc') \
            .update(status='done')
        report = order_health(self.order.id)
        self.assertEqual(report['domains']['production']['status'], 'complete')

    def test_production_is_warning_when_nothing_done_but_pending(self):
        fresh = make_order(status='planned')
        make_tasks(fresh, stations=('cut',), statuses=('pending',))
        report = order_health(fresh.id)
        self.assertEqual(report['domains']['production']['status'], 'warning')

    def test_production_is_blocked_when_all_tasks_waiting(self):
        fresh = make_order(status='planned')
        make_tasks(fresh, stations=('cut', 'cnc'), statuses=('waiting', 'waiting'))
        report = order_health(fresh.id)
        self.assertEqual(report['domains']['production']['status'], 'blocked')

    def test_production_is_stalled_after_seven_days_of_waiting(self):
        old = jdatetime.date.today() - jdatetime.timedelta(days=20)
        stale = make_order(status='planned', created_at=old)
        make_tasks(stale, stations=('cut', 'cnc'), statuses=('waiting', 'waiting'))
        report = order_health(stale.id)
        self.assertEqual(report['domains']['production']['status'], 'stalled')

    def test_order_status_is_documented_as_derived(self):
        report = order_health(self.order.id)
        self.assertIn('update_order_status', report['order_status_derived'])

    def test_unknown_station_bucket_is_always_visible(self):
        report = order_health(self.order.id)
        self.assertIn('unknown_station', report)
        self.assertIn('status', report['unknown_station'])


class DataGapQualityTests(TestCase):
    """شکاف‌های واقعی دادهٔ کارخانه باید صریح گزارش شوند."""

    def test_empty_defect_table_makes_quality_unknown_not_healthy(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('done',))
        self.assertEqual(ProductionDefect.objects.count(), 0)

        report = order_health(order.id)
        quality = report['domains']['quality']
        self.assertEqual(quality['status'], 'unknown')
        self.assertIn('missing_quality_events', quality['limitations'])
        self.assertNotEqual(quality['status'], 'healthy')

    def test_quality_reports_real_defect_when_table_has_rows(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('done',))
        make_defect(order, status='reported')

        quality = order_health(order.id)['domains']['quality']
        self.assertEqual(quality['status'], 'blocked')
        self.assertNotIn('missing_quality_events', quality['limitations'])

    def test_closed_defect_means_quality_complete(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('done',))
        make_defect(order, status='closed')

        quality = order_health(order.id)['domains']['quality']
        self.assertEqual(quality['status'], 'complete')

    def test_unmapped_materials_make_materials_insufficient_not_healthy(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('pending',))
        # دقیقاً وضعیت واقعی کارخانه: هیچ Material به RawMaterial وصل نیست.
        self.assertEqual(Material.objects.filter(raw_material__isnull=True).count(),
                         Material.objects.count())

        materials = order_health(order.id)['domains']['materials']
        self.assertEqual(materials['status'], 'insufficient_data')
        self.assertIn('missing_material_mapping', materials['limitations'])
        self.assertNotEqual(materials['status'], 'healthy')

    def test_materials_insufficient_data_never_reports_zero_shortage(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('pending',))
        materials = order_health(order.id)['domains']['materials']
        detail = materials['detail']
        self.assertEqual(detail['bom_requirements']['status'], 'insufficient_data')
        self.assertEqual(detail['bom_requirements']['reason'],
                         'missing_material_mapping')

    def test_mapped_material_allows_a_real_materials_answer(self):
        """وقتی نگاشت کامل باشد، دامنهٔ مواد دیگر insufficient_data نیست."""
        from inventory.models import RawMaterial

        order = make_order(status='producing')
        part = Part.objects.first()
        ProductionTask.objects.create(
            order=order, order_item=order.items.first(), part=part,
            station_name='cut', step_order=1, quantity=4, status='pending',
        )

        raw = RawMaterial.objects.create(
            category=self._category(), name='MDF mapped', unit='kg', pack_size=1,
        )
        for sheet in Material.objects.all():
            sheet.raw_material = raw
            sheet.save()
        self.assertEqual(Material.objects.filter(raw_material__isnull=True).count(), 0)

        materials = order_health(order.id)['domains']['materials']
        self.assertNotEqual(materials['status'], 'insufficient_data')
        self.assertTrue(materials['detail']['coverage']['mapping_complete'])

    def test_task_without_part_keeps_material_mapping_incomplete(self):
        """تسک بدون قطعه، نیاز ماده ندارد؛ پس نگاشت هنوز ناقص است."""
        from inventory.models import RawMaterial

        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('pending',))

        raw = RawMaterial.objects.create(
            category=self._category(), name='MDF mapped 2', unit='kg', pack_size=1,
        )
        for sheet in Material.objects.all():
            sheet.raw_material = raw
            sheet.save()

        materials = order_health(order.id)['domains']['materials']
        coverage = materials['detail']['coverage']
        self.assertEqual(coverage['material_rows_unmapped'], 0)
        self.assertGreater(coverage['nonpaint_tasks_unresolved'], 0)
        self.assertFalse(coverage['mapping_complete'])
        self.assertEqual(materials['status'], 'insufficient_data')

    @staticmethod
    def _category():
        from inventory.models import RawMaterialCategory
        category, _created = RawMaterialCategory.objects.get_or_create(name='ورق')
        return category


class PackagingAndShippingTests(TestCase):
    """
    نکتهٔ واقعی CraftFlow: سیگنال ``post_save`` روی ``OrderItem`` به‌طور خودکار
    ``quantity`` واحد بسته‌بندی می‌سازد؛ بنابراین «بدون واحد بسته‌بندی» فقط وقتی
    رخ می‌دهد که ``quantity`` صفر باشد.
    """

    def setUp(self):
        self.order = make_order(status='producing')
        self.item = self.order.items.first()
        make_tasks(self.order, stations=('cut',), statuses=('done',))

    def test_zero_quantity_item_has_no_packaging_units(self):
        from product.models import OrderItem, PackagingUnit, Product

        bare = make_order(status='producing', with_items=False)
        OrderItem.objects.create(
            order=bare, product=self.item.product, quantity=0, unit_price=0,
        )
        self.assertEqual(
            PackagingUnit.objects.filter(order_item__order=bare).count(), 0
        )
        report = order_health(bare.id)
        self.assertEqual(report['domains']['packaging']['status'], 'not_started')
        self.assertEqual(report['domains']['shipping']['status'], 'not_started')

    def test_auto_created_units_start_unpacked(self):
        report = order_health(self.order.id)
        packaging = report['domains']['packaging']
        self.assertEqual(packaging['detail']['shipped_units'], 0)
        self.assertEqual(packaging['status'], 'warning')

    def test_partial_packing_is_in_progress(self):
        make_packaging_units(self.item, count=3, packed=2)
        report = order_health(self.order.id)
        self.assertEqual(report['domains']['packaging']['status'], 'in_progress')
        self.assertEqual(report['domains']['shipping']['status'], 'warning')

    def test_all_packed_is_complete_and_not_shipped_is_warning(self):
        make_packaging_units(self.item, count=3, packed=3)
        report = order_health(self.order.id)
        self.assertEqual(report['domains']['packaging']['status'], 'complete')
        self.assertEqual(report['domains']['shipping']['status'], 'warning')

    def test_all_shipped_is_complete(self):
        make_packaging_units(self.item, count=3, packed=3, shipped=3)
        report = order_health(self.order.id)
        self.assertEqual(report['domains']['packaging']['status'], 'complete')
        self.assertEqual(report['domains']['shipping']['status'], 'complete')


class PaintingDomainTests(TestCase):
    def test_no_paint_tasks_is_complete(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('done',))
        painting = order_health(order.id)['domains']['painting']
        self.assertEqual(painting['status'], 'complete')
        self.assertIn('painting_required', [e['metric'] for e in painting['evidence']])

    def test_done_paint_task_is_complete(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('paint',), statuses=('done',))
        painting = order_health(order.id)['domains']['painting']
        self.assertEqual(painting['status'], 'complete')

    def test_waiting_paint_task_is_blocked(self):
        order = make_order(status='planned')
        make_tasks(order, stations=('paint',), statuses=('waiting',))
        painting = order_health(order.id)['domains']['painting']
        self.assertEqual(painting['status'], 'blocked')


class OrderDelayTests(TestCase):
    def test_invalid_order_returns_none(self):
        self.assertIsNone(order_delay(999999))

    def test_missing_due_date_forces_low_confidence(self):
        order = make_order(status='producing', due_in_days=None)
        make_tasks(order, stations=('cut',), statuses=('pending',))
        report = order_delay(order.id)
        self.assertEqual(report['confidence'], 'low')
        self.assertIn('missing_due_date', report['limitations'])
        self.assertFalse(report['due_date_available'])
        self.assertTrue(
            any('due_date' in reason for reason in report['limitations_detail'])
        )

    def test_present_due_date_is_reported_and_used(self):
        order = make_order(status='producing', due_in_days=10)
        make_tasks(order, stations=('cut',), statuses=('pending',))
        report = order_delay(order.id)
        self.assertTrue(report['due_date_available'])
        self.assertNotIn('missing_due_date', report['limitations'])

    def test_due_date_is_primary_over_age_heuristic(self):
        """سفارش قدیمی با due_date آینده نباید صرفاً به‌خاطر سن سفارش delayed شود."""
        old_created = jdatetime.date.today() - jdatetime.timedelta(days=30)
        order = make_order(
            status='producing',
            due_in_days=10,
            created_at=old_created,
        )
        tasks = make_tasks(order, stations=('cut',), statuses=('pending',))
        future = timezone.now() + timedelta(days=2)
        ProductionTask.objects.filter(pk=tasks[0].pk).update(
            scheduled_start=future,
            scheduled_end=future + timedelta(hours=4),
        )
        report = order_delay(order.id)
        self.assertFalse(report['is_delayed'])
        self.assertTrue(report['due_date_available'])
        self.assertTrue(report['delay']['due_date_used'])
        self.assertFalse(report['delay']['due_date_overdue'])

    def test_past_due_date_is_used_even_when_order_is_not_old(self):
        """due_date گذشته باید بدون اتکا به created_at به‌عنوان تأخیر دیده شود."""
        order = make_order(status='producing', due_in_days=-1, created_at=jdatetime.date.today())
        tasks = make_tasks(order, stations=('cut',), statuses=('pending',))
        future = timezone.now() + timedelta(days=2)
        ProductionTask.objects.filter(pk=tasks[0].pk).update(
            scheduled_start=future,
            scheduled_end=future + timedelta(hours=4),
        )
        report = order_delay(order.id)
        self.assertTrue(report['is_delayed'])
        self.assertEqual(report['cause_code'], 'age_only')
        self.assertTrue(report['delay']['due_date_used'])
        self.assertTrue(report['delay']['due_date_overdue'])

    def test_missing_nonpaint_schedule_is_not_hidden_by_paint_task(self):
        """وجود paint نباید زمان‌بندی ناقص cut/CNC را پنهان کند."""
        order = make_order(status='producing')
        make_tasks(
            order,
            stations=('cut', 'paint'),
            statuses=('pending', 'waiting'),
        )
        report = order_delay(order.id)
        self.assertTrue(report['is_delayed'])
        self.assertEqual(report['cause_code'], 'missing_schedule')
        self.assertIn('missing_nonpaint_schedule', report['limitations'])
        self.assertEqual(
            report['evidence'][3]['metric'],
            'due_date_overdue',
        )

    def test_completed_order_is_not_delayed(self):
        order = make_order(status='completed')
        make_tasks(order, stations=('cut',), statuses=('done',))
        report = order_delay(order.id)
        self.assertFalse(report['is_delayed'])
        self.assertIsNone(report['cause_code'])
        self.assertEqual(report['no_delay_reason'], 'order_status_completed')

    def test_no_task_breakdown_is_detected(self):
        order = make_order(status='planned', due_in_days=None)
        report = order_delay(order.id)
        self.assertTrue(report['is_delayed'])
        self.assertEqual(report['cause_code'], 'no_task_breakdown')
        self.assertEqual(report['severity'], DELAY_CAUSE_SEVERITY['no_task_breakdown'])

    def test_awaiting_predecessor_is_high_severity(self):
        """
        تقدم علت‌ها ثابت است: ``missing_schedule`` اول از همه بررسی می‌شود.
        برای رسیدن به ``awaiting_predecessor`` باید زمان‌بندی ثبت شده باشد،
        وگرنه علت درست «بدون زمان‌بندی» باقی می‌ماند.
        """
        order = make_order(status='planned')
        tasks = make_tasks(order, stations=('cut', 'cnc'), statuses=('waiting', 'waiting'))
        future = timezone.now() + timedelta(days=2)
        ProductionTask.objects.filter(pk__in=[t.pk for t in tasks]).update(
            scheduled_start=future, scheduled_end=future + timedelta(hours=4),
        )

        report = order_delay(order.id)
        self.assertEqual(report['cause_code'], 'awaiting_predecessor')
        self.assertEqual(report['severity'], 'high')
        self.assertIn('missing_dependency_graph', report['limitations'])

    def test_missing_schedule_wins_when_nothing_is_scheduled(self):
        """تقدم قطعی: نبودِ زمان‌بندی مقدم بر انتظار مرحلهٔ قبل است."""
        order = make_order(status='planned')
        make_tasks(order, stations=('cut', 'cnc'), statuses=('waiting', 'waiting'))
        report = order_delay(order.id)
        self.assertEqual(report['cause_code'], 'missing_schedule')
        self.assertEqual(report['severity'], 'medium')
        self.assertIn('missing_nonpaint_schedule', report['limitations'])

    def test_blocked_material_is_high_severity(self):
        from inventory.models import DailyMaterialQueue
        from .factories import make_raw_material, make_open_issue

        order = make_order(status='producing')
        tasks = make_tasks(order, stations=('cut',), statuses=('pending',))
        raw = make_raw_material(name='رنگ تست', stock=0)
        make_open_issue(raw, order, quantity=Decimal('8'))
        report = order_delay(order.id)
        self.assertEqual(report['cause_code'], 'blocked_material')
        self.assertEqual(report['severity'], 'high')
        self.assertTrue(report['findings'])
        self.assertTrue(report['findings'][0]['evidence'])

    def test_every_delayed_finding_carries_evidence(self):
        order = make_order(status='planned', due_in_days=None)
        make_tasks(order, stations=('cut',), statuses=('waiting',))
        report = order_delay(order.id)
        for finding in report['findings']:
            self.assertTrue(finding['evidence'])
            for evidence in finding['evidence']:
                for key in ('metric', 'value', 'unit', 'source', 'query'):
                    self.assertIn(key, evidence)
            if finding.get('derived'):
                self.assertTrue(finding['limitations'], finding['finding_id'])

    def test_severity_registry_is_fixed_and_documented(self):
        order = make_order(status='planned')
        make_tasks(order, stations=('cut',), statuses=('waiting',))
        report = order_delay(order.id)
        self.assertEqual(report['severity_registry'], DELAY_CAUSE_SEVERITY)
        self.assertEqual(tuple(report['cause_precedence']), DELAY_CAUSE_PRECEDENCE)

    def test_precedence_order_matches_the_audit(self):
        self.assertEqual(DELAY_CAUSE_PRECEDENCE, (
            'missing_schedule',
            'blocked_material',
            'blocked_quality',
            'awaiting_predecessor',
            'queue_at_station',
            'paint_not_started',
            'scheduled_overrun',
            'no_task_breakdown',
            'packaging_pending',
            'shipping_pending',
            'age_only',
        ))

    def test_delay_rule_never_uses_invented_deadline(self):
        order = make_order(status='producing', due_in_days=None)
        make_tasks(order, stations=('cut',), statuses=('pending',))
        report = order_delay(order.id)
        self.assertFalse(report['delay']['due_date_used'])
        self.assertIn('created_at', report['delay']['rule'])


class PredecessorConventionTests(TestCase):
    def test_ordinary_task_predecessor_uses_order_part_step(self):
        order = make_order()
        tasks = make_tasks(order, stations=('cut', 'cnc'))
        second = tasks[1]
        key = predecessor_key({
            'station_name': second.station_name, 'part_id': second.part_id,
            'order_item_id': second.order_item_id,
            'color_part': second.color_part, 'step_order': second.step_order,
        })
        self.assertEqual(key[0], 'std')
        self.assertEqual(key[2], second.step_order - 1)

    def test_paint_task_predecessor_uses_item_and_color(self):
        order = make_order()
        tasks = make_tasks(order, stations=('paint',))
        task = tasks[0]
        key = predecessor_key({
            'station_name': 'paint', 'part_id': task.part_id,
            'order_item_id': task.order_item_id,
            'color_part': 'بدنه', 'step_order': 3,
        })
        self.assertEqual(key, ('paint', task.order_item_id, 'بدنه', 2))

    def test_predecessor_logic_is_documented_as_derived(self):
        from craftflow_ai.analysis.limits import LIMITS
        self.assertIn('depends_on', LIMITS['missing_dependency_graph'].message)
        self.assertIn('production_dependency_graph',
                      LIMITS['missing_dependency_graph'].blocks)


class UnknownStationVisibilityTests(TestCase):
    def test_dirty_station_names_are_visible_and_not_mapped(self):
        order = make_order(status='producing')
        ProductionTask.objects.create(
            order=order, station_name='frez', step_order=9,
            quantity=1, status='pending',
        )
        ProductionTask.objects.create(
            order=order, station_name='cnc,mon', step_order=10,
            quantity=1, status='waiting',
        )

        report = order_health(order.id)
        bucket = report['unknown_station']
        self.assertEqual(bucket['status'], 'present')
        self.assertEqual(bucket['open_task_count'], 2)
        self.assertEqual(bucket['task_count'], 2)
        self.assertEqual(bucket['affected_order_count'], 1)
        self.assertIn('frez', bucket['station_names_observed'])
        self.assertIn('cnc,mon', bucket['station_names_observed'])
        self.assertIn('unknown_station_codes', bucket['limitations'])

    def test_dirty_station_names_are_never_renamed(self):
        order = make_order(status='producing')
        ProductionTask.objects.create(
            order=order, station_name='2set', step_order=9,
            quantity=1, status='pending',
        )
        bucket = order_health(order.id)['unknown_station']
        names = bucket['station_names_observed']
        self.assertIn('2set', names)
        for row in bucket['rows']:
            self.assertFalse(row['known_station'])
            self.assertIsNone(row['station_label'])


class OrderHealthRollupTests(TestCase):
    def test_unknown_domains_are_listed_separately(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('pending',))
        report = order_health(order.id)
        self.assertIn('materials', report['unknown_domains'])
        self.assertIn('quality', report['unknown_domains'])

    def test_overall_confidence_is_the_weakest_domain(self):
        order = make_order(status='producing', due_in_days=None)
        make_tasks(order, stations=('cut',), statuses=('pending',))
        report = order_health(order.id)
        confidences = {d['confidence'] for d in report['domains'].values()}
        self.assertIn(report['confidence'], confidences)
        if 'low' in confidences:
            self.assertEqual(report['confidence'], 'low')

    def test_limitations_are_reported_with_readable_detail(self):
        order = make_order(status='producing')
        make_tasks(order, stations=('cut',), statuses=('pending',))
        report = order_health(order.id)
        self.assertTrue(report['limitations'])
        self.assertEqual(len(report['limitations']),
                         len(set(report['limitations'])))
        for message in report['limitations_detail']:
            self.assertTrue(message.strip())


class EmptyDatabaseTests(TestCase):
    def test_health_on_an_empty_database_never_crashes(self):
        self.assertEqual(Order.objects.count(), 0)
        self.assertIsNone(order_health(1))
        self.assertIsNone(order_delay(1))

    def test_station_analysis_on_an_empty_database(self):
        from craftflow_ai.analysis.production import station_bottleneck
        report = station_bottleneck('paint')
        self.assertEqual(report['open_tasks'], 0)
        self.assertEqual(report['confidence'], 'low')
        self.assertEqual(report['status'], 'not_started')

    def test_invalid_stage_returns_none(self):
        from craftflow_ai.analysis.production import station_bottleneck
        self.assertIsNone(station_bottleneck('frez'))
        self.assertIsNone(station_bottleneck(''))
        self.assertIsNone(station_bottleneck(None))