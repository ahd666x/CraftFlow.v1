"""
فیلترهای صفحهٔ «کار روزانهٔ انبار».

در Engine B صفحه با فیلترهای ساختاری کار می‌کند (کارگر، ماده، وضعیت، تاریخ)
و جست‌وجوی متنی آزاد ندارد؛ دلیلش این است که ردیف صف یک «درخواست آزاد» نیست
و از برنامهٔ تولید مشتق می‌شود.

همهٔ ردیف‌های این تست از تسک‌های واقعی ساخته می‌شوند، چون هر بار که صفحه باز
می‌شود صف دوباره از برنامه مشتق می‌شود و ردیف دست‌ساز را پاک می‌کند.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory import services
from inventory.models import RawMaterial, RawMaterialCategory
from product.models import (
    Color,
    Customer,
    Order,
    OrderItem,
    PaintingMaterialRequirement,
    PaintingProcess,
    PaintingStage,
    ProductionDefect,
    ProductionTask,
    Product,
    ProductCategory,
)


class QueueFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_superuser('queueuser', password='pw')
        cls.worker_a = User.objects.create_user('worker_a', password='pw')
        cls.worker_b = User.objects.create_user('worker_b', password='pw')

        cls.raw_category = RawMaterialCategory.objects.create(name='مواد')
        cls.raw_a = RawMaterial.objects.create(
            category=cls.raw_category, name='ماده الف', code='Q001', unit='kg',
            pack_size=Decimal('0'),
        )
        cls.raw_b = RawMaterial.objects.create(
            category=cls.raw_category, name='ماده ب', code='Q002', unit='kg',
            pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول تست', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند تست', code='QFILT', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='رنگ',
            duration_minutes=30, drying_time_minutes=0,
        )
        PaintingMaterialRequirement.objects.create(
            process=cls.process, stage=cls.stage, raw_material=cls.raw_a, product=cls.product,
            color_part='بدنه', consumption_per_unit=Decimal('1.000'),
        )

        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(
            user=cls.manager, customer=cls.customer, number='QFILT-1')
        cls.order_item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=1)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)

        cls.today = timezone.localdate()

        # کارگر الف روی مادهٔ الف؛ کارگر ب هم روی همان ماده.
        cls.task_a = cls._paint_task(cls.worker_a, cls.order_item, cls.stage)
        cls.task_b = cls._paint_task(cls.worker_b, cls.order_item, cls.stage)

    @classmethod
    def _paint_task(cls, worker, order_item, stage, *, day_offset=0):
        start = timezone.localtime().replace(
            hour=8, minute=0, second=0, microsecond=0)
        return ProductionTask.objects.create(
            order=order_item.order,
            order_item=order_item,
            station_name='paint',
            painting_stage=stage,
            color_part='بدنه',
            quantity=2,
            status='pending',
            step_order=1,
            assigned_worker=worker,
            scheduled_start=start + timedelta(days=day_offset),
        )

    def setUp(self):
        self.client.force_login(self.manager)

    def test_page_renders_derived_rows(self):
        response = self.client.get(reverse('inventory:daily_material_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 2)

    def test_filter_by_worker(self):
        response = self.client.get(
            reverse('inventory:daily_material_queue'),
            {'worker': str(self.worker_b.id)},
        )
        self.assertEqual(response.status_code, 200)
        rows = list(response.context['queues'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].worker_id, self.worker_b.id)

    def test_filter_by_material(self):
        response = self.client.get(
            reverse('inventory:daily_material_queue'),
            {'material': str(self.raw_a.id)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 2)

    def test_filter_by_material_with_no_demand(self):
        response = self.client.get(
            reverse('inventory:daily_material_queue'),
            {'material': str(self.raw_b.id)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['queues']), [])

    def test_unknown_filter_values_do_not_crash(self):
        response = self.client.get(
            reverse('inventory:daily_material_queue'),
            {'worker': '999999', 'material': '999999'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['queues']), [])

    def test_rework_defect_does_not_leak_into_other_days(self):
        """
        نیاز جبران خرابی باید فقط در روزِ ``material_work_date`` خودش ظاهر شود.
        """
        yesterday = self.today - timedelta(days=1)
        ProductionDefect.objects.create(
            order=self.order,
            order_item=self.order_item,
            color_part='بدنه',
            quantity=1,
            description='خرابی دیروز',
            reported_by=self.manager,
            status='material_requested',
            material_raw_material=self.raw_b,
            material_quantity=Decimal('5'),
            material_worker=self.worker_a,
            material_work_date=yesterday,
        )

        services.sync_queue_for_date(self.today)
        services.sync_queue_for_date(yesterday)

        today_qty = services.daily_queue_summary(
            services.daily_queue_filtered_queryset(self.today))['total_planned']
        yesterday_qty = services.daily_queue_summary(
            services.daily_queue_filtered_queryset(yesterday))['total_planned']

        # فقط نیاز نقاشیِ امروز؛ نیاز جبرانیِ دیروز نباید اضافه شود.
        self.assertEqual(today_qty, Decimal('4.00'))
        self.assertEqual(yesterday_qty, Decimal('5.00'))