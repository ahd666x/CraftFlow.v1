"""
P2 — یکپارچه‌سازی ``RawMaterial`` واقعی با ``PaintingMaterialRequirement``.

زنجیرهٔ هدف:
    PaintingMaterialRequirement → RawMaterial → StockMovement

نکتهٔ معماری: ``PaintingMaterialRequirement.raw_material`` از قبل یک FK واقعی
به ``inventory.RawMaterial`` است، اما برای اسلات‌های رنگ‌وابسته مقدار ذخیره‌شده
یک ماده نماینده است و مادهٔ واقعی فقط از ``PaintingColorMaterialVariant``
به‌دست می‌آید. P2 این تبدیل را در یک resolver واحد canonical می‌کند
(``product.utils.resolve_painting_raw_material``) و تضمین می‌کند صف روزانه فقط
از مادهٔ واقعی استفاده کند.

این فایل عمداً هیچ assertion مربوط به رفتار P1 (de-duplication) را تغییر
نمی‌دهد؛ فقط ثابت می‌کند de-duplication روی مادهٔ درست انجام می‌شود.
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from inventory import services
from inventory.models import (
    DailyMaterialQueue,
    DailyMaterialQueueSource,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)
from product.models import (
    Color,
    Customer,
    Order,
    OrderItem,
    PaintingColorMaterialVariant,
    PaintingMaterialRequirement,
    PaintingProcess,
    PaintingProcessMaterial,
    PaintingStage,
    Product,
    ProductCategory,
    ProductionTask,
)
from product.utils import (
    PaintingMaterialResolutionError,
    get_resolved_painting_requirements_for_task,
    resolve_painting_raw_material,
    resolve_painting_raw_material_with_note,
)

CONSUMPTION = Decimal('1.300')


class RawMaterialChainBase(TestCase):
    """زیرساخت مشترک: یک روند با یک اسلات رنگ‌وابسته و یک ماده ثابت."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser('p2admin', password='pw')
        cls.worker = User.objects.create_user('p2_worker', password='pw')

        cls.category = RawMaterialCategory.objects.create(name='مواد نقاشی P2')

        # ماده ثابت (غیر رنگ‌وابسته): خودش ماده واقعی است
        cls.thinner = RawMaterial.objects.create(
            category=cls.category, name='تینر P2', code='P2-THIN',
            unit='lit', pack_size=Decimal('0'),
        )
        # اسلات رنگ‌وابسته: ماده نماینده، نه ماده قابل تحویل
        cls.slot = RawMaterial.objects.create(
            category=cls.category, name='اسلات رنگ P2', code='P2-SLOT',
            unit='lit', pack_size=Decimal('0'),
        )
        # ماده‌های واقعی هر کد رنگ
        cls.paint_code_8 = RawMaterial.objects.create(
            category=cls.category, name='رنگ واقعی کد ۸ P2', code='P2-C8',
            unit='lit', pack_size=Decimal('0'),
        )
        cls.paint_code_9 = RawMaterial.objects.create(
            category=cls.category, name='رنگ واقعی کد ۹ P2', code='P2-C9',
            unit='lit', pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته P2')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول P2', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند P2', code='P2', color_codes=['8', '9'], is_active=True,
        )
        cls.stages = [
            PaintingStage.objects.create(
                process=cls.process, order=i, name='مرحله %s' % i,
                duration_minutes=30, drying_time_minutes=0,
            )
            for i in range(1, 4)
        ]

        PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.thinner,
        )
        cls.slot_entry = PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.slot, is_color_variant=True,
        )
        cls.variant_8 = PaintingColorMaterialVariant.objects.create(
            process_material=cls.slot_entry, color_code='8',
            raw_material=cls.paint_code_8,
        )
        cls.variant_9 = PaintingColorMaterialVariant.objects.create(
            process_material=cls.slot_entry, color_code='9',
            raw_material=cls.paint_code_9,
        )

        # فرمول: ماده ثابت + اسلات رنگ‌وابسته
        cls.thinner_req = PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.thinner,
            product=cls.product, color_part='بدنه',
            consumption_per_unit=Decimal('0.400'),
        )
        cls.slot_req = PaintingMaterialRequirement.objects.create(
            process=cls.process, raw_material=cls.slot,
            product=cls.product, color_part='بدنه',
            consumption_per_unit=CONSUMPTION,
        )

        cls.customer = Customer.objects.create(name='مشتری P2', phone='09120000001')
        cls.order = Order.objects.create(
            user=cls.superuser, customer=cls.customer, number='P2-1',
        )

        cls.day1 = timezone.localdate()
        cls.day_start = timezone.make_aware(
            timezone.datetime.combine(cls.day1, time(8, 0))
        )

    def _make_item(self, code='8', color_part='بدنه', quantity=1):
        item = OrderItem.objects.create(
            order=self.order, product=self.product, quantity=quantity,
        )
        Color.objects.create(part=color_part, code=code, orderitem=item)
        return item

    def _stage_tasks(self, item, worker=None, *, stages=None, start=None, quantity=1):
        created = []
        for step, stage in enumerate(stages or self.stages, start=1):
            created.append(ProductionTask.objects.create(
                order=self.order,
                order_item=item,
                station_name='paint',
                step_order=step,
                quantity=quantity,
                status='pending',
                painting_stage=stage,
                color_part='بدنه',
                assigned_worker=worker or self.worker,
                scheduled_start=start or self.day_start,
            ))
        return created

    def _queue(self, raw, worker=None, work_date=None):
        return DailyMaterialQueue.objects.filter(
            work_date=work_date or self.day1,
            worker=worker or self.worker,
            raw_material=raw,
        ).first()


# ---------------------------------------------------------------------------
# Test 1 — requirement با RawMaterial معتبر
# ---------------------------------------------------------------------------
class ValidRawMaterialTests(RawMaterialChainBase):
    def test_01_requirement_resolves_to_the_real_raw_material(self):
        """مادهٔ ثابت خودش ماده واقعی است؛ اسلات به ماده واقعی کد رنگ می‌رسد."""
        item = self._make_item(code='8')
        task = self._stage_tasks(item, stages=self.stages[:1])[0]

        resolved, errors, context = get_resolved_painting_requirements_for_task(task)

        self.assertEqual(errors, [])
        self.assertEqual(context['color_code'], '8')
        self.assertEqual(len(resolved), 2)
        by_requirement = {
            entry['requirement'].pk: entry['raw_material'] for entry in resolved
        }
        self.assertEqual(by_requirement[self.thinner_req.pk], self.thinner)
        self.assertEqual(by_requirement[self.slot_req.pk], self.paint_code_8)

    def test_02_queue_uses_the_real_raw_material_not_the_slot(self):
        """queue باید ماده واقعی بگیرد، نه اسلات رنگ‌وابسته."""
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])

        queue = self._queue(self.paint_code_8)
        self.assertIsNotNone(queue, 'صف باید روی ماده واقعی ساخته شود.')
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertIsNone(self._queue(self.slot))

    def test_03_thinner_queue_keeps_its_own_material(self):
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])

        thinner_queue = self._queue(self.thinner)
        self.assertIsNotNone(thinner_queue)
        self.assertEqual(thinner_queue.planned_quantity, Decimal('0.40'))

    def test_04_source_traceability_points_at_the_real_material(self):
        """DailyMaterialQueueSource باید همان ماده واقعی صف را نگه دارد."""
        item = self._make_item(code='8')
        tasks = self._stage_tasks(item, stages=self.stages[:1])

        queue = self._queue(self.paint_code_8)
        source = queue.sources.get()
        self.assertEqual(source.raw_material, self.paint_code_8)
        self.assertEqual(source.raw_material, queue.raw_material)
        self.assertEqual(source.production_task_id, tasks[0].id)

    def test_05_stock_movement_chain_is_reachable_from_the_real_material(self):
        """زنجیره تا StockMovement باز است: همان RawMaterial صف حرکت انبار می‌گیرد."""
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])
        queue = self._queue(self.paint_code_8)

        StockMovement.objects.create(
            raw_material=self.paint_code_8, movement_type='purchase',
            quantity=Decimal('50'), created_by=self.superuser,
        )

        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        queue.refresh_from_db()
        self.assertEqual(queue.raw_material, self.paint_code_8)
        self.assertTrue(
            StockMovement.objects.filter(
                raw_material=self.paint_code_8, movement_type='consumption',
            ).exists()
        )
        # مادهٔ اسلات هرگز نباید حرکت انبار بگیرد.
        self.assertFalse(
            StockMovement.objects.filter(raw_material=self.slot).exists()
        )


# ---------------------------------------------------------------------------
# Test 2 — دو requirement با ماده متفاوت → دو queue جدا
# ---------------------------------------------------------------------------
class DistinctMaterialTests(RawMaterialChainBase):
    def test_06_two_requirements_produce_two_distinct_queues(self):
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])

        queues = DailyMaterialQueue.objects.filter(
            work_date=self.day1, worker=self.worker,
        ).exclude(status='cancelled')
        self.assertEqual(queues.count(), 2)
        self.assertEqual(
            {q.raw_material_id for q in queues},
            {self.thinner.pk, self.paint_code_8.pk},
        )

    def test_07_different_color_codes_resolve_to_different_real_materials(self):
        """کد رنگ متفاوت ⇒ ماده واقعی متفاوت ⇒ صف جدا برای هر کد."""
        item_8 = self._make_item(code='8')
        item_9 = self._make_item(code='9')
        self._stage_tasks(item_8, stages=self.stages[:1])
        self._stage_tasks(item_9, stages=self.stages[:1])

        queue_8 = self._queue(self.paint_code_8)
        queue_9 = self._queue(self.paint_code_9)
        self.assertIsNotNone(queue_8)
        self.assertIsNotNone(queue_9)
        self.assertNotEqual(queue_8.pk, queue_9.pk)
        self.assertEqual(queue_8.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue_9.planned_quantity, Decimal('1.30'))
        # هیچ صفی روی اسلات ساخته نشده است.
        self.assertFalse(
            DailyMaterialQueue.objects.filter(raw_material=self.slot).exists()
        )

    def test_08_one_material_must_not_appear_as_another(self):
        """ماده کد ۹ نباید در صف کد ۸ ثبت شود."""
        item_8 = self._make_item(code='8')
        self._stage_tasks(item_8, stages=self.stages[:1])

        queue_8 = self._queue(self.paint_code_8)
        self.assertIsNotNone(queue_8)
        self.assertNotIn(queue_8.raw_material_id, {self.slot.pk, self.paint_code_9.pk})


# ---------------------------------------------------------------------------
# Test 3 — mapping نامعتبر: نه صف با ماده اشتباه، نه NULL/fake
# ---------------------------------------------------------------------------
class MissingMappingTests(RawMaterialChainBase):
    def test_09_missing_variant_creates_no_queue_and_reports_the_reason(self):
        """بدون نگاشت برای کد رنگ، صف ساخته نمی‌شود و خطا قابل ردیابی است."""
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])
        self.assertIsNotNone(self._queue(self.paint_code_8))

        # تغییر کد رنگ سفارش به کدی که mapping ندارد
        self.variant_8.delete()

        task = ProductionTask.objects.filter(station_name='paint').first()
        resolved, errors, context = get_resolved_painting_requirements_for_task(task)

        self.assertEqual(len(resolved), 1, 'فقط مادهٔ ثابت باید resolve شود.')
        self.assertEqual(resolved[0]['raw_material'], self.thinner)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, 'missing_color_variant')
        self.assertEqual(errors[0].color_code, '8')
        self.assertEqual(errors[0].slot_raw_material_id, self.slot.pk)

        services.sync_daily_material_queue(self.day1)

        # صف رنگ لغو شده و هرگز به مادهٔ اشتباه منتقل نشده.
        self.assertEqual(self._queue(self.paint_code_8).status, 'cancelled')
        self.assertEqual(self._queue(self.slot), None)
        for queue in DailyMaterialQueue.objects.filter(work_date=self.day1):
            self.assertNotIn(
                queue.raw_material_id,
                {self.slot.pk, self.paint_code_9.pk},
            )

    def test_10_missing_variant_reaches_queue_diagnostics(self):
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])
        self.variant_8.delete()

        tasks = services._get_scheduled_painting_tasks(self.day1)
        grouped, diagnostics = services.aggregate_queue_requirements(tasks)

        unresolved = diagnostics['unresolved_materials']
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]['reason'], 'missing_color_variant')
        self.assertEqual(unresolved[0]['slot_raw_material_id'], self.slot.pk)
        self.assertEqual(unresolved[0]['color_code'], '8')
        self.assertTrue(unresolved[0]['label'])

        # نیاز ماده ثابت هنوز درست resolve می‌شود، پس تسک skip نمی‌شود؛ فقط
        # نیاز رنگ گزارش می‌شود و هیچ صفی روی اسلات ساخته نمی‌شود.
        self.assertEqual(diagnostics['skipped'], [])
        self.assertIn((self.worker.pk, self.thinner.pk), grouped)
        self.assertNotIn((self.worker.pk, self.slot.pk), grouped)

    def test_10b_task_is_skipped_when_no_requirement_resolves(self):
        """وقتی هیچ نیازی resolve نشود، تسک skip و علت گزارش می‌شود."""
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])
        self.variant_8.delete()
        self.thinner_req.delete()

        tasks = services._get_scheduled_painting_tasks(self.day1)
        grouped, diagnostics = services.aggregate_queue_requirements(tasks)

        self.assertEqual(grouped, {})
        self.assertEqual(len(diagnostics['unresolved_materials']), 1)
        skipped_reasons = {s['reason'] for s in diagnostics['skipped']}
        self.assertEqual(skipped_reasons, {'unresolved_material'})
        self.assertEqual(
            diagnostics['skipped'][0]['label'],
            services._SKIP_REASONS['unresolved_material'],
        )

    def test_11_material_outside_process_catalog_is_still_resolved_but_reported(self):
        """
        مادهٔ خارج از کاتالوگ روند، یک FK واقعی است؛ نیاز حذف نمی‌شود ولی نقص
        کاتالوگ گزارش می‌شود (این رفتارِ موجودِ پروژه حفظ شده است).
        """
        outsider = RawMaterial.objects.create(
            category=self.category, name='ماده بیرون کاتالوگ P2',
            code='P2-OUT', unit='lit',
        )
        req = PaintingMaterialRequirement.objects.create(
            process=self.process, raw_material=outsider,
            product=self.product, color_part='پایه',
            consumption_per_unit=Decimal('1.000'),
        )

        raw, note = resolve_painting_raw_material_with_note(
            req, process=self.process, color_code='8',
        )

        self.assertEqual(raw, outsider)
        self.assertIsNotNone(note)
        self.assertEqual(note.reason, 'material_outside_process_catalog')
        self.assertEqual(note.as_dict()['slot_raw_material_id'], outsider.pk)

    def test_11b_material_outside_catalog_reaches_queue_diagnostics(self):
        outsider = RawMaterial.objects.create(
            category=self.category, name='ماده بیرون کاتالوگ P2',
            code='P2-OUT', unit='lit',
        )
        PaintingMaterialRequirement.objects.create(
            process=self.process, raw_material=outsider,
            product=self.product, color_part='بدنه',
            consumption_per_unit=Decimal('0.500'),
        )
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])

        tasks = services._get_scheduled_painting_tasks(self.day1)
        grouped, diagnostics = services.aggregate_queue_requirements(tasks)

        self.assertIn((self.worker.pk, outsider.pk), grouped)
        self.assertEqual(len(diagnostics['mapping_notes']), 1)
        self.assertEqual(
            diagnostics['mapping_notes'][0]['reason'],
            'material_outside_process_catalog',
        )
        self.assertEqual(diagnostics['unresolved_materials'], [])

    def test_12_missing_color_code_is_not_resolved_to_the_slot(self):
        """بدون کد رنگ، اسلات رنگ‌وابسته نباید به خودش برگردد."""
        item = self._make_item(code='8')
        task = self._stage_tasks(item, stages=self.stages[:1])[0]
        Color.objects.filter(orderitem=item).update(code='nan')

        resolved, errors, context = get_resolved_painting_requirements_for_task(task)

        self.assertEqual(context['color_code'], None)
        self.assertEqual([e.reason for e in errors], ['missing_color_code'])
        for entry in resolved:
            self.assertNotEqual(entry['raw_material'], self.slot)


# ---------------------------------------------------------------------------
# Test 4 — P1 روی ماده واقعی درست کار می‌کند
# ---------------------------------------------------------------------------
class P1DedupWithRealMaterialTests(RawMaterialChainBase):
    def test_13_three_stages_of_one_work_unit_count_once(self):
        item = self._make_item(code='8')
        tasks = self._stage_tasks(item)

        queue = self._queue(self.paint_code_8)
        self.assertEqual(queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue.sources.count(), len(tasks))
        self.assertEqual(
            sum((s.quantity for s in queue.sources.all()), Decimal('0')),
            queue.planned_quantity,
        )

    def test_14_p1_grouping_keys_use_the_real_material(self):
        """کلید واحد کار نقاشی و گروه صف باید روی ماده واقعی ساخته شوند."""
        item = self._make_item(code='8')
        task = self._stage_tasks(item, stages=self.stages[:1])[0]

        grouped, _diagnostics = services.aggregate_queue_requirements([task])

        self.assertIn((self.worker.pk, self.paint_code_8.pk), grouped)
        self.assertNotIn((self.worker.pk, self.slot.pk), grouped)
        unit_key = services._painting_work_unit_key(task, self.paint_code_8.pk)
        self.assertEqual(unit_key[-1], self.paint_code_8.pk)

        queue = self._queue(self.paint_code_8)
        self.assertEqual(queue.raw_material_id, self.paint_code_8.pk)

    def test_15_two_colour_codes_do_not_merge_into_one_queue(self):
        """دو کد رنگ = دو ماده واقعی = دو صف مستقل با مقدار کامل هر کد."""
        item_8 = self._make_item(code='8')
        item_9 = self._make_item(code='9')
        self._stage_tasks(item_8, stages=self.stages[:1])
        self._stage_tasks(item_9, stages=self.stages[:1])

        queue_8 = self._queue(self.paint_code_8)
        queue_9 = self._queue(self.paint_code_9)
        self.assertEqual(queue_8.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue_9.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue_8.sources.count(), 1)
        self.assertEqual(queue_9.sources.count(), 1)

    def test_16_two_workers_on_different_materials_stay_separate(self):
        worker_b = User.objects.create_user('p2_worker_b', password='pw')
        item_8 = self._make_item(code='8')
        item_9 = self._make_item(code='9')
        self._stage_tasks(item_8, worker=self.worker, stages=self.stages[:1])
        self._stage_tasks(item_9, worker=worker_b, stages=self.stages[:1])

        queue_a = self._queue(self.paint_code_8, worker=self.worker)
        queue_b = self._queue(self.paint_code_9, worker=worker_b)
        self.assertEqual(queue_a.planned_quantity, Decimal('1.30'))
        self.assertEqual(queue_b.planned_quantity, Decimal('1.30'))


# ---------------------------------------------------------------------------
# Test 5 — تغییر mapping و اثر آن روی صف
# ---------------------------------------------------------------------------
class MappingChangeTests(RawMaterialChainBase):
    def test_17_mapping_change_moves_the_queue_to_the_new_material(self):
        """با تغییر mapping و sync مجدد، صف باید ماده واقعی جدید را بگیرد."""
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])
        self.assertIsNotNone(self._queue(self.paint_code_8))

        self.variant_8.raw_material = self.paint_code_9
        self.variant_8.save()

        services.sync_daily_material_queue(self.day1)

        old_queue = self._queue(self.paint_code_8)
        new_queue = self._queue(self.paint_code_9)
        self.assertEqual(old_queue.status, 'cancelled')
        self.assertEqual(old_queue.sources.count(), 0)
        self.assertEqual(new_queue.status, 'pending')
        self.assertEqual(new_queue.planned_quantity, Decimal('1.30'))
        self.assertEqual(new_queue.sources.get().raw_material, self.paint_code_9)

    def test_18_mapping_change_after_delivery_keeps_history_and_flags_conflict(self):
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])
        StockMovement.objects.create(
            raw_material=self.paint_code_8, movement_type='purchase',
            quantity=Decimal('50'), created_by=self.superuser,
        )
        queue = self._queue(self.paint_code_8)
        services.execute_daily_delivery(queue_id=queue.pk, delivered_by=self.superuser)

        self.variant_8.raw_material = self.paint_code_9
        self.variant_8.save()
        services.sync_daily_material_queue(self.day1)

        queue.refresh_from_db()
        self.assertEqual(queue.raw_material, self.paint_code_8)
        self.assertEqual(queue.delivered_quantity, Decimal('1.30'))
        self.assertTrue(queue.has_plan_conflict)
        new_queue = self._queue(self.paint_code_9)
        self.assertEqual(new_queue.planned_quantity, Decimal('1.30'))

    def test_19_mapping_removal_cancels_the_queue_without_faking_material(self):
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])
        self.assertIsNotNone(self._queue(self.paint_code_8))

        self.variant_8.delete()
        services.sync_daily_material_queue(self.day1)

        self.assertEqual(self._queue(self.paint_code_8).status, 'cancelled')
        self.assertFalse(
            DailyMaterialQueue.objects.filter(
                work_date=self.day1, raw_material__in=[self.slot, self.paint_code_9]
            ).exists()
        )


# ---------------------------------------------------------------------------
# Test 6 — traceability منبع صف
# ---------------------------------------------------------------------------
class QueueSourceTraceabilityTests(RawMaterialChainBase):
    def test_20_every_source_matches_its_queue_material(self):
        item = self._make_item(code='8')
        self._stage_tasks(item)

        for queue in DailyMaterialQueue.objects.filter(
            work_date=self.day1, status__in=['pending', 'delivered'],
        ):
            for source in queue.sources.all():
                self.assertEqual(source.raw_material_id, queue.raw_material_id)

    def test_21_sources_stay_linked_to_task_stage_and_material(self):
        item = self._make_item(code='8')
        tasks = self._stage_tasks(item)

        queue = self._queue(self.paint_code_8)
        sources = DailyMaterialQueueSource.objects.filter(queue=queue)
        self.assertEqual({s.production_task_id for s in sources},
                         {t.id for t in tasks})
        self.assertEqual({s.painting_stage_id for s in sources},
                         {s.pk for s in self.stages})
        self.assertEqual({s.raw_material_id for s in sources},
                         {self.paint_code_8.pk})

    def test_22_report_traceability_uses_the_real_material(self):
        item = self._make_item(code='8')
        self._stage_tasks(item)

        from inventory import reports
        report = reports.order_material_traceability(self.order)

        material_ids = {row['raw_material_id'] for row in report['rows']}
        self.assertIn(self.paint_code_8.pk, material_ids)
        self.assertNotIn(self.slot.pk, material_ids)
        self.assertEqual(report['totals']['planned'], Decimal('1.70'))

    def test_23_requirement_row_in_db_is_never_rewritten_by_sync(self):
        """resolver نباید FK ذخیره‌شدهٔ requirement را تغییر دهد."""
        item = self._make_item(code='8')
        self._stage_tasks(item, stages=self.stages[:1])

        services.sync_daily_material_queue(self.day1)

        self.slot_req.refresh_from_db()
        self.assertEqual(self.slot_req.raw_material_id, self.slot.pk)

    def test_24_legacy_resolver_helper_agrees_with_the_canonical_one(self):
        """تابع سازگاری قدیمی باید همان ماده واقعی resolver را برگرداند."""
        item = self._make_item(code='8')
        task = self._stage_tasks(item, stages=self.stages[:1])[0]

        from product.utils import get_painting_material_requirements_for_task
        legacy = get_painting_material_requirements_for_task(task)
        resolved, errors, _context = get_resolved_painting_requirements_for_task(task)

        self.assertEqual(errors, [])
        self.assertEqual(
            {r.raw_material_id for r in legacy},
            {entry['raw_material'].pk for entry in resolved},
        )