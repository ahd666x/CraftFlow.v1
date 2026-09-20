from django.test import TestCase
from django.contrib.auth.models import Group, User
from django.urls import reverse
from django.db import transaction
from decimal import Decimal
from datetime import datetime, time
from unittest.mock import patch
from django.utils import timezone
import json
import jdatetime

from product.utils import (
    is_working_day,
    consume_material_for_task,
    consume_material_for_paint_task,
    get_painting_material_requirements_for_item_colorpart,
    auto_create_material_issues,
)
from product.models import (
    Product, ProductCategory, ProductBOM, Part, Material,
    Order, OrderItem, Color, ProductionTask, ProductionEvent, Customer, WorkerProfile,
    PaintingProcess, PaintingStage, PaintingMaterialRequirement,
    PaintingProcessMaterial, ProductionDefect, PackagingUnit,
    PaintingColorMaterialVariant, STATION_CHOICES, ProductionLog,
)
from inventory.models import (
    RawMaterial, RawMaterialCategory, StockMovement, MaterialIssue,
)
from inventory.views import _task_material_requirements


class IsWorkingDayTests(TestCase):
    def test_friday_is_not_working_day(self):
        friday = jdatetime.date.today()
        while friday.weekday() != 6:  # جمعه
            friday = jdatetime.date.fromgregorian(date=friday.togregorian() + __import__('datetime').timedelta(days=1))
        with patch('product.utils.Holiday.objects.filter') as mock_holiday:
            mock_holiday.return_value.exists.return_value = False
            self.assertFalse(is_working_day(friday))

    def test_saturday_is_working_day(self):
        saturday = jdatetime.date.today()
        while saturday.weekday() != 0:  # شنبه
            saturday = jdatetime.date.fromgregorian(date=saturday.togregorian() + __import__('datetime').timedelta(days=1))
        with patch('product.utils.Holiday.objects.filter') as mock_holiday:
            mock_holiday.return_value.exists.return_value = False
            self.assertTrue(is_working_day(saturday))

    def test_holiday_is_not_working_day(self):
        saturday = jdatetime.date.today()
        while saturday.weekday() != 0:
            saturday = jdatetime.date.fromgregorian(date=saturday.togregorian() + __import__('datetime').timedelta(days=1))
        with patch('product.utils.Holiday.objects.filter') as mock_holiday:
            mock_holiday.return_value.exists.return_value = True
            self.assertFalse(is_working_day(saturday))

    def test_wednesday_is_working_day(self):
        wednesday = jdatetime.date.today()
        while wednesday.weekday() != 3:  # چهارشنبه
            wednesday = jdatetime.date.fromgregorian(date=wednesday.togregorian() + __import__('datetime').timedelta(days=1))
        with patch('product.utils.Holiday.objects.filter') as mock_holiday:
            mock_holiday.return_value.exists.return_value = False
            self.assertTrue(is_working_day(wednesday))


# ============================================================
# تست‌های مدل‌های جدید (PaintingProcessMaterial و PaintingMaterialRequirement با process FK)
# ============================================================

class PaintingProcessMaterialModelTests(TestCase):
    """تست‌های مدل کاتالوگ مواد روند نقاشی"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('testuser2', password='testpass')
        
        cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cat, name='رنگ تست', code='P002', unit='lit',
            min_stock_alert=Decimal('10.00'),
        )
        cls.thinner_raw = RawMaterial.objects.create(
            category=cat, name='تینر تست', code='T002', unit='lit',
            min_stock_alert=Decimal('5.00'),
        )
        
        cls.process1 = PaintingProcess.objects.create(
            name='روند تست 1', code='TST1', color_codes=['8'],
            is_active=True,
        )
        cls.process2 = PaintingProcess.objects.create(
            name='روند تست 2', code='TST2', color_codes=['9'],
            is_active=True,
        )

    def test_create_process_material(self):
        """ایجاد запиس کاتالوگ برای یک روند"""
        from product.models import PaintingProcessMaterial
        
        entry = PaintingProcessMaterial.objects.create(
            process=self.process1,
            raw_material=self.paint_raw,
        )
        
        self.assertEqual(entry.process, self.process1)
        self.assertEqual(entry.raw_material, self.paint_raw)
        self.assertEqual(str(entry), f"{self.process1.name} ← {self.paint_raw.name}")

    def test_unique_constraint_process_material(self):
        """تست یکتای بودن ترکیب process + raw_material"""
        from product.models import PaintingProcessMaterial
        from django.db import IntegrityError, transaction
        
        PaintingProcessMaterial.objects.create(
            process=self.process1,
            raw_material=self.paint_raw,
        )
        
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaintingProcessMaterial.objects.create(
                    process=self.process1,
                    raw_material=self.paint_raw,
                )

    def test_same_material_different_processes(self):
        """ماده یکسان می‌تواند در روندهای مختلف باشد"""
        from product.models import PaintingProcessMaterial
        
        PaintingProcessMaterial.objects.create(
            process=self.process1,
            raw_material=self.paint_raw,
        )
        entry2 = PaintingProcessMaterial.objects.create(
            process=self.process2,
            raw_material=self.paint_raw,
        )
        
        self.assertEqual(PaintingProcessMaterial.objects.count(), 2)
        self.assertEqual(entry2.process, self.process2)


class PaintingMaterialRequirementNewModelTests(TestCase):
    """تست‌های مدل جدید PaintingMaterialRequirement (با process FK)"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('testuser3', password='testpass')
        
        cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cat, name='رنگ تست جدید', code='P003', unit='lit',
            min_stock_alert=Decimal('10.00'),
        )
        cls.thinner_raw = RawMaterial.objects.create(
            category=cat, name='تینر تست جدید', code='T003', unit='lit',
            min_stock_alert=Decimal('5.00'),
        )
        
        cls.category = ProductCategory.objects.create(name='تست دسته')
        cls.product = Product.objects.create(
            category=cls.category, name='محصول تست', base_price=1000,
        )
        
        cls.process = PaintingProcess.objects.create(
            name='روند اصلی', code='MAIN', color_codes=['8'],
            is_active=True,
        )
        
        # Create catalog entry first
        from product.models import PaintingProcessMaterial
        cls.catalog_entry = PaintingProcessMaterial.objects.create(
            process=cls.process,
            raw_material=cls.paint_raw,
        )

    def test_create_requirement_with_process(self):
        """ایجاد requirement با process FK (نه painting_stage)"""
        from product.models import PaintingMaterialRequirement
        
        req = PaintingMaterialRequirement.objects.create(
            process=self.process,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.250'),
        )
        
        self.assertEqual(req.process, self.process)
        self.assertEqual(req.raw_material, self.paint_raw)
        self.assertEqual(req.product, self.product)
        self.assertEqual(req.color_part, 'بدنه')
        self.assertEqual(req.consumption_per_unit, Decimal('0.250'))

    def test_requirement_requires_catalog_entry(self):
        """Requirement بایدKatlog entry داشته باشد"""
        from product.models import PaintingMaterialRequirement
        from django.core.exceptions import ValidationError
        
        # This should work - catalog exists
        req = PaintingMaterialRequirement(
            process=self.process,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.250'),
        )
        req.full_clean()  # Should not raise
        req.save()

    def test_unique_constraint_process_material_product_colorpart(self):
        """تست یکتای بودن ترکیب process + raw_material + product + color_part"""
        from product.models import PaintingMaterialRequirement
        from django.db import IntegrityError, transaction
        
        PaintingMaterialRequirement.objects.create(
            process=self.process,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.250'),
        )
        
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaintingMaterialRequirement.objects.create(
                    process=self.process,
                    raw_material=self.paint_raw,
                    product=self.product,
                    color_part='بدنه',
                    consumption_per_unit=Decimal('0.300'),
                )

    def test_different_color_part_allowed(self):
        """color_part‌های مختلف برای همان process+material+product مجازند"""
        from product.models import PaintingMaterialRequirement
        
        req1 = PaintingMaterialRequirement.objects.create(
            process=self.process,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.250'),
        )
        req2 = PaintingMaterialRequirement.objects.create(
            process=self.process,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='درب',
            consumption_per_unit=Decimal('0.300'),
        )
        
        self.assertEqual(PaintingMaterialRequirement.objects.count(), 2)
        self.assertEqual(req1.color_part, 'بدنه')
        self.assertEqual(req2.color_part, 'درب')

    def test_product_and_colorpart_required(self):
        """product و color_part الزامی هستند (nullable=False)"""
        from product.models import PaintingMaterialRequirement
        from django.db import IntegrityError
        
        # This should fail at database level (NOT NULL constraint)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaintingMaterialRequirement.objects.create(
                    process=self.process,
                    raw_material=self.paint_raw,
                    product=None,
                    color_part='بدنه',
                    consumption_per_unit=Decimal('0.250'),
                )


class PaintingProcessMaterialsAPITests(TestCase):
    """تست‌های API کاتالوگ مواد روند نقاشی"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('apiuser', password='testpass')
        
        cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cat, name='رنگ API', code='API01', unit='lit',
        )
        cls.thinner_raw = RawMaterial.objects.create(
            category=cat, name='تینر API', code='API02', unit='lit',
        )
        
        cls.process = PaintingProcess.objects.create(
            name='روند API', code='API', color_codes=['8'],
            is_active=True,
        )

    def setUp(self):
        self.client.login(username='apiuser', password='testpass')

    def test_get_empty_catalog(self):
        """GET کاتالوگ خالی"""
        url = reverse('painting_process_materials_api', args=[self.process.id])
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['materials'], [])

    def test_post_add_material_to_catalog(self):
        """POST اضافه کردن ماده به کاتالوگ"""
        from product.models import PaintingProcessMaterial
        
        url = reverse('painting_process_materials_api', args=[self.process.id])
        response = self.client.post(
            url,
            data=json.dumps({'raw_material_id': self.paint_raw.id}),
            content_type='application/json',
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['created'])
        
        # Verify in database
        entry = PaintingProcessMaterial.objects.get(process=self.process, raw_material=self.paint_raw)
        self.assertEqual(entry.id, data['id'])

    def test_post_duplicate_material_not_created(self):
        """POST ماده تکراری ایجاد نمی‌کند (get_or_create)"""
        from product.models import PaintingProcessMaterial
        
        PaintingProcessMaterial.objects.create(
            process=self.process, raw_material=self.paint_raw,
        )
        
        url = reverse('painting_process_materials_api', args=[self.process.id])
        response = self.client.post(
            url,
            data=json.dumps({'raw_material_id': self.paint_raw.id}),
            content_type='application/json',
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['created'])  # Not created, already existed
        
        self.assertEqual(PaintingProcessMaterial.objects.count(), 1)

    def test_delete_material_from_catalog(self):
        """DELETE حذف ماده از کاتالوگ"""
        from product.models import PaintingProcessMaterial, PaintingMaterialRequirement
        
        entry = PaintingProcessMaterial.objects.create(
            process=self.process, raw_material=self.paint_raw,
        )
        
        url = reverse('painting_process_materials_api', args=[self.process.id])
        response = self.client.delete(f"{url}?id={entry.id}")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        # Verify deleted
        self.assertFalse(PaintingProcessMaterial.objects.filter(id=entry.id).exists())


class ProductColorPartMaterialsAPITests(TestCase):
    """تست‌های API مدیریت مقدار مصرف برای محصول + بخش رنگی"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('apiuser2', password='testpass')
        
        cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cat, name='رنگ API2', code='API21', unit='lit',
        )
        cls.thinner_raw = RawMaterial.objects.create(
            category=cat, name='تینر API2', code='API22', unit='lit',
        )
        
        cls.category = ProductCategory.objects.create(name='تست دسته 2')
        cls.product = Product.objects.create(
            category=cls.category, name='محصول API', base_price=1000,
        )
        
        cls.process1 = PaintingProcess.objects.create(
            name='روند 1', code='P1', color_codes=['8'], is_active=True,
        )
        cls.process2 = PaintingProcess.objects.create(
            name='روند 2', code='P2', color_codes=['9'], is_active=True,
        )
        
        # Create catalog entries
        from product.models import PaintingProcessMaterial
        cls.catalog1 = PaintingProcessMaterial.objects.create(
            process=cls.process1, raw_material=cls.paint_raw,
        )
        cls.catalog2 = PaintingProcessMaterial.objects.create(
            process=cls.process1, raw_material=cls.thinner_raw,
        )
        cls.catalog3 = PaintingProcessMaterial.objects.create(
            process=cls.process2, raw_material=cls.paint_raw,
        )

    def setUp(self):
        self.client.login(username='apiuser2', password='testpass')

    def test_get_requirements_returns_all_catalog_entries(self):
        """GET باید تمام ورودی‌های کاتالوگ برای روندهای فعال را برگرداند"""
        from django.urls import reverse
        
        url = reverse('product_color_part_materials_api')
        response = self.client.get(f"{url}?product_id={self.product.id}&color_part=بدنه")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        # Should return 3 rows (one per catalog entry)
        self.assertEqual(len(data['rows']), 3)
        
        # Check structure
        for row in data['rows']:
            self.assertIn('process_id', row)
            self.assertIn('process_name', row)
            self.assertIn('raw_material_id', row)
            self.assertIn('raw_material_name', row)
            self.assertIn('unit', row)
            self.assertIn('consumption_per_unit', row)
            self.assertIn('is_set', row)
            self.assertIn('requirement_id', row)
            self.assertFalse(row['is_set'])  # None set yet
            self.assertIsNone(row['requirement_id'])

    def test_get_requirements_with_existing_values(self):
        """GET با مقادیر موجود"""
        from product.models import PaintingMaterialRequirement
        from django.urls import reverse
        
        # Create existing requirement
        req = PaintingMaterialRequirement.objects.create(
            process=self.process1,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.500'),
        )
        
        url = reverse('product_color_part_materials_api')
        response = self.client.get(f"{url}?product_id={self.product.id}&color_part=بدنه")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        # Find the row for process1 + paint_raw
        target_row = next(r for r in data['rows'] 
                         if r['process_id'] == self.process1.id 
                         and r['raw_material_id'] == self.paint_raw.id)
        
        self.assertTrue(target_row['is_set'])
        self.assertEqual(target_row['consumption_per_unit'], '0.500')
        self.assertEqual(target_row['requirement_id'], req.id)

    def test_post_set_consumption(self):
        """POST تعیین مقدار مصرف"""
        from product.models import PaintingMaterialRequirement
        from django.urls import reverse
        
        url = reverse('product_color_part_materials_api')
        response = self.client.post(
            url,
            data=json.dumps({
                'product_id': self.product.id,
                'color_part': 'بدنه',
                'process_id': self.process1.id,
                'raw_material_id': self.paint_raw.id,
                'consumption_per_unit': '0.750',
            }),
            content_type='application/json',
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        # Verify in database
        req = PaintingMaterialRequirement.objects.get(
            process=self.process1,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
        )
        self.assertEqual(req.consumption_per_unit, Decimal('0.750'))
        self.assertEqual(req.id, data['id'])

    def test_post_update_existing_consumption(self):
        """POST به‌روزرسانی مقدار موجود"""
        from product.models import PaintingMaterialRequirement
        from django.urls import reverse
        
        req = PaintingMaterialRequirement.objects.create(
            process=self.process1,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.100'),
        )
        
        url = reverse('product_color_part_materials_api')
        response = self.client.post(
            url,
            data=json.dumps({
                'product_id': self.product.id,
                'color_part': 'بدنه',
                'process_id': self.process1.id,
                'raw_material_id': self.paint_raw.id,
                'consumption_per_unit': '0.999',
            }),
            content_type='application/json',
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        req.refresh_from_db()
        self.assertEqual(req.consumption_per_unit, Decimal('0.999'))

    def test_post_requires_catalog_entry(self):
        """POST برای ماده‌ای که در کاتالوگ نیست خطا می‌دهد"""
        from django.urls import reverse
        
        # Create a process without catalog entry for thinner
        process3 = PaintingProcess.objects.create(
            name='روند 3', code='P3', color_codes=['7'], is_active=True,
        )
        
        url = reverse('product_color_part_materials_api')
        response = self.client.post(
            url,
            data=json.dumps({
                'product_id': self.product.id,
                'color_part': 'بدنه',
                'process_id': process3.id,
                'raw_material_id': self.thinner_raw.id,  # Not in catalog for process3
                'consumption_per_unit': '0.500',
            }),
            content_type='application/json',
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('کاتالوگ', data['error'])

    def test_delete_requirement(self):
        """DELETE حذف مقدار مصرف (بازگشت به حالت تعیین‌نشده)"""
        from product.models import PaintingMaterialRequirement
        from django.urls import reverse
        
        req = PaintingMaterialRequirement.objects.create(
            process=self.process1,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.500'),
        )
        
        url = reverse('product_color_part_materials_api')
        response = self.client.delete(f"{url}?id={req.id}")
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        
        # Verify deleted
        self.assertFalse(PaintingMaterialRequirement.objects.filter(id=req.id).exists())


class PaintingMaterialRequirementUtilsTests(TestCase):
    """تست‌های توابع کمکی برای مدل جدید"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('utiluser', password='testpass')
        
        cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cat, name='رنگ Utils', code='UTL01', unit='lit',
        )
        
        cls.category = ProductCategory.objects.create(name='تست Utils')
        cls.product = Product.objects.create(
            category=cls.category, name='محصول Utils', base_price=1000,
        )
        
        cls.process = PaintingProcess.objects.create(
            name='روند Utils', code='UTL', color_codes=['8'], is_active=True,
        )
        
        from product.models import PaintingProcessMaterial, PaintingStage
        cls.catalog = PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.paint_raw,
        )
        
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='مرحله Utils',
            duration_minutes=30, drying_time_minutes=60, required_skill='painter',
        )

    def test_get_painting_material_requirements_for_task_new_structure(self):
        """get_painting_material_requirements_for_task با مدل جدید"""
        from product.utils import get_painting_material_requirements_for_task
        from product.models import PaintingMaterialRequirement, ProductionTask, Order, OrderItem, Color, Customer
        
        # Create requirement for product + color_part
        PaintingMaterialRequirement.objects.create(
            process=self.process,
            raw_material=self.paint_raw,
            product=self.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.333'),
        )
        
        # Create order item with color
        customer = Customer.objects.create(name='مشتری Utils', phone='09120000000')
        order = Order.objects.create(user=self.user, customer=customer, number='ORDUTL')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=3)
        Color.objects.create(part='بدنه', code='8', orderitem=item)
        
        # Create paint task (using stage, but function should resolve to process)
        task = ProductionTask.objects.create(
            order=order, part=None, station_name='paint', step_order=10,
            quantity=3, status='pending', painting_stage=self.stage,
            order_item=item, color_part='بدنه',
        )
        
        reqs = get_painting_material_requirements_for_task(task)
        
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].raw_material, self.paint_raw)
        self.assertEqual(reqs[0].consumption_per_unit, Decimal('0.333'))
        self.assertEqual(reqs[0].process, self.process)
        self.assertEqual(reqs[0].product, self.product)
        self.assertEqual(reqs[0].color_part, 'بدنه')


class MaterialIssueStockMovementLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('stocklinkuser', password='testpass')
        cls.cat = RawMaterialCategory.objects.create(name='مواد اولیه تست')
        cls.production_raw = RawMaterial.objects.create(category=cls.cat, name='ماده تولید', unit='kg')
        cls.rework_raw = RawMaterial.objects.create(category=cls.cat, name='ماده خرابی', unit='kg')
        StockMovement.objects.create(
            raw_material=cls.production_raw, movement_type='purchase',
            quantity=Decimal('100'), note='موجودی اولیه',
        )
        StockMovement.objects.create(
            raw_material=cls.rework_raw, movement_type='purchase',
            quantity=Decimal('100'), note='موجودی اولیه',
        )

        cls.product_category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(category=cls.product_category, name='محصول تست', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='MATLINK')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        cls.process = PaintingProcess.objects.create(name='روند تست', code='MATLINK', color_codes=['8'], is_active=True)
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='مرحله تست',
            duration_minutes=10, drying_time_minutes=0, required_skill='painter',
        )
        cls.task = ProductionTask.objects.create(
            order=cls.order, part=None, station_name='paint', step_order=1,
            quantity=2, status='pending', painting_stage=cls.stage,
            order_item=cls.order_item, color_part='بدنه',
        )

    def setUp(self):
        self.client.login(username='stocklinkuser', password='testpass')

    def test_issue_material_links_stock_movement(self):
        issue = MaterialIssue.objects.create(
            task=self.task, raw_material=self.production_raw,
            requested_quantity=Decimal('5'), status='requested', purpose='production',
            requested_by=self.user,
        )
        response = self.client.post(reverse('inventory:issue_material', args=[issue.id]), {'quantity': '3'})
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertIsNotNone(issue.stock_movement)
        self.assertEqual(issue.stock_movement.reference_task_id, issue.task_id)
        self.assertEqual(issue.stock_movement.quantity, Decimal('3'))
        self.assertEqual(issue.issued_quantity, Decimal('3'))
        self.assertEqual(issue.status, 'partial')

    def test_rework_movement_excluded_from_normal_consumption_report(self):
        rework_movement = StockMovement.objects.create(
            raw_material=self.rework_raw, movement_type='consumption', quantity=Decimal('2'),
            reference_task=self.task, note=f'تحویل انبار #rework — جبران خرابی / ساخت مجدد',
        )
        defect = ProductionDefect.objects.create(
            task=self.task, order=self.order, order_item=self.order_item,
            color_part='بدنه', quantity=1, description='خرابی تست',
            reported_by=self.user,
        )
        MaterialIssue.objects.create(
            task=self.task, defect=defect, raw_material=self.rework_raw,
            requested_quantity=Decimal('2'), issued_quantity=Decimal('2'),
            purpose='rework', status='issued', requested_by=self.user,
            stock_movement=rework_movement,
        )
        response = self.client.get(reverse('report_material_consumption'))
        self.assertEqual(response.status_code, 200)
        rows = response.context['report_rows']
        self.assertEqual(len(rows), 1)
        self.assertNotIn(self.rework_raw.id, rows[0]['materials'])
        self.assertIn(self.rework_raw.id, rows[0]['defect_materials'])
        defect_totals = response.context['defect_material_totals']
        self.assertEqual(len(defect_totals), 1)
        self.assertEqual(defect_totals[0]['raw_material'], self.rework_raw)
        self.assertEqual(defect_totals[0]['qty'], Decimal('2'))

    def test_production_movement_still_included_in_report(self):
        movement = StockMovement.objects.create(
            raw_material=self.production_raw, movement_type='consumption', quantity=Decimal('4'),
            reference_task=self.task, note=f'تحویل انبار #production — برنامه تولید',
        )
        MaterialIssue.objects.create(
            task=self.task, raw_material=self.production_raw,
            requested_quantity=Decimal('4'), issued_quantity=Decimal('4'),
            purpose='production', status='issued', requested_by=self.user,
            stock_movement=movement,
        )
        response = self.client.get(reverse('report_material_consumption'))
        self.assertEqual(response.status_code, 200)
        rows = response.context['report_rows']
        self.assertEqual(len(rows), 1)
        self.assertIn(self.production_raw.id, rows[0]['materials'])
        self.assertEqual(rows[0]['materials'][self.production_raw.id]['qty'], Decimal('4'))

    def test_report_date_filters_accept_persian_dates(self):
        movement = StockMovement.objects.create(
            raw_material=self.production_raw,
            movement_type='consumption',
            quantity=Decimal('3'),
            reference_task=self.task,
        )
        movement_date = jdatetime.date(1402, 10, 12).togregorian()
        StockMovement.objects.filter(pk=movement.pk).update(
            created_at=timezone.make_aware(datetime.combine(movement_date, time(12)))
        )

        response = self.client.get(reverse('report_material_consumption'), {
            'date_from': '1402/10/12',
            'date_to': '1402/10/12',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['report_rows']), 1)
        self.assertIn(self.production_raw.id, response.context['report_rows'][0]['materials'])

    def test_backfill_command_links_unambiguous_old_movement(self):
        from django.core.management import call_command
        issue = MaterialIssue.objects.create(
            task=self.task, raw_material=self.rework_raw,
            requested_quantity=Decimal('1'), issued_quantity=Decimal('1'),
            purpose='rework', status='issued', requested_by=self.user,
        )
        movement = StockMovement.objects.create(
            raw_material=self.rework_raw, movement_type='consumption', quantity=Decimal('1'),
            reference_task=self.task, note=f'تحویل انبار #{issue.id} — جبران خرابی / ساخت مجدد',
        )
        call_command('backfill_material_issue_links', verbosity=0)
        issue.refresh_from_db()
        self.assertEqual(issue.stock_movement, movement)

    def test_task_without_part_does_not_fallback_to_product_bom(self):
        material = Material.objects.create(
            name='ماده فرمول', thickness=Decimal('1'), raw_material=self.production_raw,
            consumption_per_unit=Decimal('1'),
        )
        part = Part.objects.create(
            material=material, name='قطعه فرمول', length=Decimal('1'), width=Decimal('1'),
            pname='محصول تست', routing_code='test',
        )
        ProductBOM.objects.create(product=self.product, part=part, quantity=2)
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='mon',
            quantity=1, status='pending', step_order=2,
        )
        self.assertEqual(_task_material_requirements(task), [])


class PackagingUnitDefectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('unituser', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته واحد')
        cls.product = Product.objects.create(category=cls.category, name='محصول واحد', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری واحد', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='UNIT')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=1)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        cls.unit = PackagingUnit.objects.get(order_item=cls.order_item, unit_number=1)
        cls.raw_material = RawMaterial.objects.create(
            name='ماده واحد', unit='kg', category=RawMaterialCategory.objects.create(name='واحد')
        )
        cls.process = PaintingProcess.objects.create(
            name='روند واحد', code='UNIT', color_codes=['8'], is_active=True
        )

    def setUp(self):
        self.client.login(username='unituser', password='testpass')

    def test_defect_form_links_unit_and_material_issue(self):
        response = self.client.post(reverse('product_defects'), {
            'order_id': self.order.id,
            'packaging_unit_id': self.unit.id,
            'color_part': 'بدنه',
            'quantity': '1',
            'description': 'خرابی واحد',
            'raw_material_id': self.raw_material.id,
            'replacement_quantity': '2',
        })
        self.assertEqual(response.status_code, 302)
        defect = ProductionDefect.objects.get(packaging_unit=self.unit)
        issue = MaterialIssue.objects.get(defect=defect)
        self.assertEqual(defect.status, 'material_requested')
        self.assertEqual(issue.packaging_unit, self.unit)
        self.assertEqual(issue.purpose, 'rework')

    def test_cancel_material_issue_ajax(self):
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, packaging_unit=self.unit,
            color_part='بدنه', quantity=1, description='خرابی واحد', reported_by=self.user,
            status='material_requested',
        )
        issue = MaterialIssue.objects.create(
            defect=defect, packaging_unit=self.unit, raw_material=self.raw_material,
            requested_quantity=Decimal('2'), purpose='rework', status='requested',
            requested_by=self.user,
        )
        response = self.client.post(
            reverse('inventory:cancel_material_issue', args=[issue.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        issue.refresh_from_db()
        defect.refresh_from_db()
        self.assertEqual(issue.status, 'cancelled')
        self.assertEqual(defect.status, 'reported')

    def test_ajax_order_units_and_color_parts(self):
        units_response = self.client.get(
            reverse('ajax_order_units_for_defect', args=[self.order.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(units_response.status_code, 200)
        self.assertEqual(units_response.json()['results'][0]['id'], self.unit.id)
        parts_response = self.client.get(
            reverse('ajax_unit_color_parts_for_defect', args=[self.unit.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(parts_response.status_code, 200)
        self.assertEqual(parts_response.json()['results'], [{'value': 'بدنه', 'text': 'بدنه'}])

    def test_scan_packaging_unit_defect_branch_links_unit(self):
        WorkerProfile.objects.create(user=self.user, stage='mon')
        response = self.client.post(reverse('scan_packaging_unit', args=[self.unit.id]), {
            'color_part': 'بدنه',
            'quantity': '1',
            'description': 'خرابی اسکن',
        })
        self.assertEqual(response.status_code, 302)
        defect = ProductionDefect.objects.get(packaging_unit=self.unit)
        self.assertEqual(defect.order_item, self.order_item)
        self.assertEqual(defect.color_part, 'بدنه')
        self.assertEqual(defect.quantity, 1)

    def test_paint_scan_opens_material_request_for_reported_defect(self):
        WorkerProfile.objects.create(user=self.user, stage='paint')
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, packaging_unit=self.unit,
            color_part='بدنه', quantity=1, description='خط و خش',
            reported_by=self.user, status='reported',
        )

        response = self.client.get(reverse('scan_packaging_unit', args=[self.unit.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'درخواست مواد برای جبران خرابی')
        self.assertContains(response, defect.description)
        self.assertContains(response, self.raw_material.name)

    def test_paint_scan_creates_rework_material_issue(self):
        WorkerProfile.objects.create(user=self.user, stage='paint')
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, packaging_unit=self.unit,
            color_part='بدنه', quantity=1, description='خط و خش',
            reported_by=self.user, status='reported',
        )

        response = self.client.post(reverse('scan_packaging_unit', args=[self.unit.id]), {
            'defect_ids': [str(defect.id)],
            f'raw_material_{defect.id}': str(self.raw_material.id),
            f'quantity_{defect.id}': '2.5',
        })

        self.assertEqual(response.status_code, 302)
        issue = MaterialIssue.objects.get(defect=defect)
        defect.refresh_from_db()
        self.assertEqual(issue.raw_material, self.raw_material)
        self.assertEqual(issue.requested_quantity, Decimal('2.5'))
        self.assertEqual(issue.purpose, 'rework')
        self.assertEqual(issue.status, 'requested')
        self.assertEqual(issue.packaging_unit, self.unit)
        self.assertEqual(issue.order_item, self.order_item)
        self.assertEqual(defect.status, 'material_requested')

    def test_paint_scan_without_reported_defect_shows_empty_state(self):
        WorkerProfile.objects.create(user=self.user, stage='paint')

        response = self.client.get(reverse('scan_packaging_unit', args=[self.unit.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'هیچ خرابی ثبت‌شده‌ای برای این واحد وجود ندارد')

    def test_warehouse_user_can_access_material_queue(self):
        warehouse_group = Group.objects.get_or_create(name='انبار')[0]
        warehouse_user = User.objects.create_user('warehouseuser', password='testpass')
        warehouse_user.groups.add(warehouse_group)
        WorkerProfile.objects.create(user=warehouse_user, stage='paint')
        self.client.login(username='warehouseuser', password='testpass')

        response = self.client.get(reverse('inventory:production_issue_queue'))

        self.assertEqual(response.status_code, 200)

    def test_report_uses_actual_color_process_without_task(self):
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, packaging_unit=self.unit,
            color_part='بدنه', quantity=1, description='خرابی روند', reported_by=self.user,
        )
        response = self.client.get(reverse('report_material_consumption'), {'process': self.process.id})
        self.assertEqual(response.status_code, 200)
        rows = response.context['report_rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['process'], self.process)
        self.assertEqual(rows[0]['defect_count'], 1)

    def test_production_queue_renders_packaging_unit_issue(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='mon',
            quantity=1, status='pending', step_order=1,
        )
        issue = MaterialIssue.objects.create(
            task=task, packaging_unit=self.unit, raw_material=self.raw_material,
            requested_quantity=Decimal('1'), purpose='production', status='requested',
            requested_by=self.user,
        )
        response = self.client.get(reverse('inventory:production_issue_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'بارکد #{self.unit.id}')
        self.assertContains(response, str(issue.id))

    def test_station_filter_includes_rework_through_defect_task(self):
        task = ProductionTask.objects.create(
            order=self.order, order_item=self.order_item, station_name='mon',
            quantity=1, status='pending', step_order=1,
        )
        defect = ProductionDefect.objects.create(
            task=task, order=self.order, order_item=self.order_item,
            packaging_unit=self.unit, color_part='بدنه', quantity=1,
            description='خرابی ایستگاه', reported_by=self.user,
        )
        issue = MaterialIssue.objects.create(
            defect=defect, packaging_unit=self.unit, raw_material=self.raw_material,
            requested_quantity=Decimal('1'), purpose='rework', status='requested',
            requested_by=self.user,
        )
        response = self.client.get(reverse('inventory:production_issue_queue'), {'station': 'mon'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(issue.id))


# ============================================================
# تست‌های مربوط به بازطراحی صف تحویل مواد نقاشی
# ============================================================

class PaintMaterialIssuePerItemTests(TestCase):
    """تست‌های درخواست مواد نقاشی در سطح (OrderItem + color_part)"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('paintuser', password='testpass')
        
        cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.paint_raw = RawMaterial.objects.create(
            category=cat, name='رنگ تست', code='P001', unit='lit',
            min_stock_alert=Decimal('10.00'),
        )
        cls.thinner_raw = RawMaterial.objects.create(
            category=cat, name='تینر تست', code='T001', unit='lit',
            min_stock_alert=Decimal('5.00'),
        )
        
        cls.category = ProductCategory.objects.create(name='تست دسته')
        cls.product = Product.objects.create(
            category=cls.category, name='کنسول اوربیتال', base_price=1000,
        )
        
        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='ORD001')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=5)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        
        cls.process = PaintingProcess.objects.create(
            name='پوششی', code='POSH', color_codes=['8'], is_active=True,
        )
        cls.stage1 = PaintingStage.objects.create(
            process=cls.process, order=1, name='دهان اولیه',
            duration_minutes=30, drying_time_minutes=60, required_skill='painter',
        )
        cls.stage2 = PaintingStage.objects.create(
            process=cls.process, order=2, name='کد رنگ',
            duration_minutes=30, drying_time_minutes=60, required_skill='painter',
        )
        
        # Create catalog entries
        from product.models import PaintingProcessMaterial
        cls.catalog_paint = PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.paint_raw,
        )
        cls.catalog_thinner = PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.thinner_raw,
        )
        
        # Create requirements
        cls.req_paint = PaintingMaterialRequirement.objects.create(
            process=cls.process,
            raw_material=cls.paint_raw,
            product=cls.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.200'),
        )
        cls.req_thinner = PaintingMaterialRequirement.objects.create(
            process=cls.process,
            raw_material=cls.thinner_raw,
            product=cls.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.050'),
        )
        
        # Create paint tasks (two stages for same order_item + color_part)
        cls.task1 = ProductionTask.objects.create(
            order=cls.order, order_item=cls.order_item, station_name='paint', step_order=10,
            quantity=5, status='pending', painting_stage=cls.stage1,
            color_part='بدنه',
        )
        cls.task2 = ProductionTask.objects.create(
            order=cls.order, order_item=cls.order_item, station_name='paint', step_order=20,
            quantity=5, status='pending', painting_stage=cls.stage2,
            color_part='بدنه',
        )

    def test_paint_material_issue_created_once_per_item_colorpart(self):
        """تست اینکه برای هر (order_item, color_part) فقط یک درخواست مواد ایجاد می‌شود"""
        auto_create_material_issues([self.task1, self.task2], requested_by=self.user)
        
        # باید فقط 2 MaterialIssue وجود داشته باشد (رنگ و تینر)، نه 4 (2 تسک × 2 ماده)
        issues = MaterialIssue.objects.filter(
            purpose='production',
            task__isnull=True,
            order_item=self.order_item,
            color_part='بدنه',
        )
        self.assertEqual(issues.count(), 2)
        
        # بررسی مقادیر: quantity = item.quantity * consumption_per_unit
        paint_issue = issues.get(raw_material=self.paint_raw)
        thinner_issue = issues.get(raw_material=self.thinner_raw)
        
        self.assertEqual(paint_issue.requested_quantity, Decimal('1.000'))  # 5 * 0.200
        self.assertEqual(thinner_issue.requested_quantity, Decimal('0.250'))  # 5 * 0.050

    def test_paint_material_issue_quantity_matches_item_quantity(self):
        """تست اینکه requested_quantity برابر item.quantity * consumption_per_unit است"""
        auto_create_material_issues([self.task1, self.task2], requested_by=self.user)
        
        paint_issue = MaterialIssue.objects.get(
            order_item=self.order_item, color_part='بدنه', raw_material=self.paint_raw
        )
        # item.quantity = 5, consumption_per_unit = 0.200
        expected = Decimal('5') * Decimal('0.200')
        self.assertEqual(paint_issue.requested_quantity, expected)

    def test_paint_material_issue_task_is_none(self):
        """تست اینکه برای درخواست‌های نقاشی جدید task برابر None است"""
        auto_create_material_issues([self.task1], requested_by=self.user)
        
        issue = MaterialIssue.objects.get(
            order_item=self.order_item, color_part='بدنه', raw_material=self.paint_raw
        )
        self.assertIsNone(issue.task)
        self.assertEqual(issue.order_item, self.order_item)
        self.assertEqual(issue.color_part, 'بدنه')
        self.assertEqual(issue.painting_process, self.process)

    def test_issue_material_for_paint_creates_stock_movement_with_order_item_ref(self):
        """تست اینکه تحویل مواد برای نقاشی StockMovement با reference_order_item ایجاد می‌کند"""
        auto_create_material_issues([self.task1], requested_by=self.user)
        
        issue = MaterialIssue.objects.get(
            order_item=self.order_item, color_part='بدنه', raw_material=self.paint_raw
        )
        
        # اضافه کردن موجودی انبار
        StockMovement.objects.create(
            raw_material=self.paint_raw, movement_type='purchase',
            quantity=Decimal('100'), note='موجودی اولیه',
        )
        
        # شبیه‌سازی issue_material view
        from django.test import Client
        client = Client()
        client.login(username='paintuser', password='testpass')
        response = client.post(reverse('inventory:issue_material', args=[issue.id]), {'quantity': '1'})
        
        issue.refresh_from_db()
        self.assertIsNotNone(issue.stock_movement)
        movement = issue.stock_movement
        
        self.assertIsNone(movement.reference_task)
        self.assertEqual(movement.reference_order_item, self.order_item)
        self.assertEqual(movement.reference_color_part, 'بدنه')

    def test_report_material_consumption_includes_new_style_movements(self):
        """تست اینکه گزارش مصرف مواد درخواست‌های جدید را شامل می‌شود"""
        auto_create_material_issues([self.task1], requested_by=self.user)
        
        issue = MaterialIssue.objects.get(
            order_item=self.order_item, color_part='بدنه', raw_material=self.paint_raw
        )
        
        # اضافه کردن موجودی انبار
        StockMovement.objects.create(
            raw_material=self.paint_raw, movement_type='purchase',
            quantity=Decimal('100'), note='موجودی اولیه',
        )
        
        # تحویل مواد
        from django.test import Client
        client = Client()
        client.login(username='paintuser', password='testpass')
        client.post(reverse('inventory:issue_material', args=[issue.id]), {'quantity': '1'})
        
        # گزارش
        response = client.get(reverse('report_material_consumption'))
        self.assertEqual(response.status_code, 200)
        rows = response.context['report_rows']
        
        # باید ردیفی برای این محصول و روند وجود داشته باشد
        found = False
        for row in rows:
            if row['product'] == self.product and row['process'] == self.process:
                found = True
                self.assertIn(self.paint_raw.id, row['materials'])
                self.assertEqual(row['materials'][self.paint_raw.id]['qty'], Decimal('1'))
        self.assertTrue(found, 'گزارش باید درخواست‌های جدید نقاشی را نمایش دهد')


class ConsolidatePaintMaterialIssuesCommandTests(TestCase):
    """تست‌های دستور ادغام درخواست‌های مواد نقاشی"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('consolidateuser', password='testpass')
        
        cat = RawMaterialCategory.objects.create(name='رنگ و مواد')
        cls.paint_raw = RawMaterial.objects.create(
            category=cat, name='رنگ', code='P002', unit='lit',
        )
        
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(
            category=cls.category, name='محصول', base_price=1000,
        )
        
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='ORD002')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=3)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        
        cls.process = PaintingProcess.objects.create(
            name='روند', code='PRC', color_codes=['8'], is_active=True,
        )
        cls.stage1 = PaintingStage.objects.create(
            process=cls.process, order=1, name='مرحله 1',
            duration_minutes=30, drying_time_minutes=60, required_skill='painter',
        )
        cls.stage2 = PaintingStage.objects.create(
            process=cls.process, order=2, name='مرحله 2',
            duration_minutes=30, drying_time_minutes=60, required_skill='painter',
        )
        
        from product.models import PaintingProcessMaterial
        cls.catalog_paint = PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.paint_raw,
        )
        
        cls.req_paint = PaintingMaterialRequirement.objects.create(
            process=cls.process,
            raw_material=cls.paint_raw,
            product=cls.product,
            color_part='بدنه',
            consumption_per_unit=Decimal('0.100'),
        )
        
        # دو تسک قدیمی (per-task)
        cls.task1 = ProductionTask.objects.create(
            order=cls.order, order_item=cls.order_item, station_name='paint', step_order=10,
            quantity=3, status='pending', painting_stage=cls.stage1,
            color_part='بدنه',
        )
        cls.task2 = ProductionTask.objects.create(
            order=cls.order, order_item=cls.order_item, station_name='paint', step_order=20,
            quantity=3, status='pending', painting_stage=cls.stage2,
            color_part='بدنه',
        )
        
        # دو MaterialIssue قدیمی برای هر تسک
        cls.old_issue1 = MaterialIssue.objects.create(
            task=cls.task1, raw_material=cls.paint_raw,
            requested_quantity=Decimal('0.300'), purpose='production', status='requested',
            requested_by=cls.user,
        )
        cls.old_issue2 = MaterialIssue.objects.create(
            task=cls.task2, raw_material=cls.paint_raw,
            requested_quantity=Decimal('0.300'), purpose='production', status='requested',
            requested_by=cls.user,
        )

    def test_consolidate_command_merges_duplicates(self):
        """تست اینکه دستور ادغام درخواست‌های تکراری قدیمی را می‌کند"""
        from django.core.management import call_command
        from io import StringIO
        
        out = StringIO()
        call_command('consolidate_paint_material_issues', '--dry-run', stdout=out)
        output = out.getvalue()
        
        self.assertIn('ادغام', output)
        
        # اجرای واقعی
        out = StringIO()
        call_command('consolidate_paint_material_issues', stdout=out)
        output = out.getvalue()
        
        # باید فقط یک درخواست فعال باقی بماند
        active_issues = MaterialIssue.objects.filter(
            purpose='production',
            status__in=['requested', 'partial', 'issued'],
            order_item=self.order_item,
            color_part='بدنه',
            raw_material=self.paint_raw,
            task__isnull=True,
        )
        self.assertEqual(active_issues.count(), 1)
        
        # درخواست‌های قدیمی باید cancelled باشند
        cancelled_issues = MaterialIssue.objects.filter(
            id__in=[self.old_issue1.id, self.old_issue2.id],
            status='cancelled',
        )
        self.assertEqual(cancelled_issues.count(), 2)
        
        # مقدار درخواست جدید باید درست باشد: 3 * 0.100 = 0.300
        new_issue = active_issues.first()
        self.assertEqual(new_issue.requested_quantity, Decimal('0.300'))


class ReportStagesNPlusOneTests(TestCase):
    """Regression guard: `report_stages` must not issue per-item/per-stage queries."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            'stagesadmin', 'a@b.c', 'pass', first_name='ادمین', last_name='گزارش'
        )
        cls.category = ProductCategory.objects.create(name='دسته تست')
        cls.product = Product.objects.create(
            category=cls.category, name='محصول تست', base_price=1000
        )
        cls.customer = Customer.objects.create(name='مشتری تست', phone='09120000000')

    def setUp(self):
        self.client.login(username='stagesadmin', password='pass')

    def _seed(self, count):
        """Create `count` order items, each with a log at every production stage
        and one packaging unit (packed but not shipped). quantity=1 keeps the
        auto-generated QR-code signal affordable in test runtime."""
        production_stations = [c for c, _ in STATION_CHOICES
                               if c not in ('packaging', 'shipping')]
        for i in range(count):
            order = Order.objects.create(
                user=self.user, customer=self.customer, number=f'S{count}_{i}'
            )
            oi = OrderItem.objects.create(order=order, product=self.product, quantity=1)
            for stage in production_stations:
                ProductionLog.objects.create(
                    order_item=oi, stage=stage, user=self.user, notes='seed'
                )
            pu = oi.packaging_units.first()
            pu.is_packed = True
            pu.packed_at = '2024-01-01T10:00:00Z'
            pu.save(update_fields=['is_packed', 'packed_at'])
        return list(OrderItem.objects.order_by('-id'))

    def test_query_count_is_bounded_regardless_of_item_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        url = reverse('report_stages')

        # Small dataset (fits a single page of 200).
        self._seed(30)
        with CaptureQueriesContext(connection) as ctx_small:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        small_q = len(ctx_small)

        # Larger dataset (still rendered on page 1 of the 200-per-page view).
        # Query count must NOT scale with the number of matched rows.
        self._seed(120)
        with CaptureQueriesContext(connection) as ctx_large:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        large_q = len(ctx_large)

        # Bounded well below 20 and independent of the row count.
        self.assertLess(large_q, 20, f'report_stages issued {large_q} queries')
        self.assertEqual(small_q, large_q,
                         'query count must not grow with the number of items')

    def test_output_matches_model_state(self):
        """Stage dates, summary counts and packaging stats must be correct."""
        items = self._seed(10)
        resp = self.client.get(reverse('report_stages'))
        self.assertEqual(resp.status_code, 200)

        context = resp.context
        report_data = context['report_data']

        # Every row present and ordered by -id.
        self.assertEqual(len(report_data), len(items))
        self.assertEqual(
            [r['item'].id for r in report_data],
            [i.id for i in items[:len(report_data)]],
        )

        first = report_data[0]['item']
        # Stage status: one entry per station, formatted as MM/DD or None.
        stage_status = report_data[0]['stage_status']
        production_stations = [c for c, _ in STATION_CHOICES
                               if c not in ('packaging', 'shipping')]
        self.assertEqual(set(stage_status.keys()), set(code for code, _ in STATION_CHOICES))
        for stage in production_stations:
            log = ProductionLog.objects.filter(order_item=first, stage=stage).first()
            if log and log.created_at:
                expected = f"{log.created_at.month:02d}/{log.created_at.day:02d}"
                self.assertEqual(stage_status[stage], expected)
            else:
                self.assertIsNone(stage_status[stage])

        # Packaging stats must equal the model's own progress helpers.
        packed, total = first.packaging_progress
        shipped, _ = first.shipping_progress
        row = report_data[0]
        self.assertEqual(row['total_units'], total)
        self.assertEqual(row['packed_units'], packed)
        self.assertEqual(row['shipped_units'], shipped)

        # Summary "done" counts must equal distinct item counts per stage.
        base_ids = set(OrderItem.objects.values_list('pk', flat=True))
        for code, _ in STATION_CHOICES:
            if code in ('packaging', 'shipping'):
                continue
            expected_done = (
                ProductionLog.objects
                .filter(order_item_id__in=base_ids, stage=code)
                .values('order_item_id')
                .distinct()
                .count()
            )
            self.assertEqual(context['summary'][code]['done'], expected_done)
        self.assertEqual(context['summary']['cut']['total'], len(items))

        # Pagination context exposed for the template.
        self.assertIn('page_obj', context)
        self.assertEqual(context['page_obj'].paginator.per_page, 200)

    def test_in_stock_filter_returns_packed_unshipped_units(self):
        items = self._seed(3)
        shipped = items[0].packaging_units.first()
        shipped.is_shipped = True
        shipped.save(update_fields=['is_shipped'])

        unpacked = items[1].packaging_units.first()
        unpacked.is_packed = False
        unpacked.save(update_fields=['is_packed'])

        response = self.client.get(
            reverse('report_stages'), {'packaging_status': 'in_stock'}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row['item'].id for row in response.context['report_data']},
            {items[2].id},
        )
        self.assertEqual(response.context['report_data'][0]['in_stock_units'], 1)

    def test_packed_filter_returns_items_with_any_packed_unit(self):
        items = self._seed(2)
        unpacked = items[0].packaging_units.first()
        unpacked.is_packed = False
        unpacked.save(update_fields=['is_packed'])

        response = self.client.get(
            reverse('report_stages'), {'packaging_status': 'packed'}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row['item'].id for row in response.context['report_data']},
            {items[1].id},
        )

    def test_representative_falls_back_to_username_when_full_name_is_empty(self):
        representative = User.objects.create_user(
            'representative-user', password='pass'
        )
        order = Order.objects.create(
            user=representative,
            customer=self.customer,
            number='S-REPRESENTATIVE',
        )
        item = OrderItem.objects.create(
            order=order, product=self.product, quantity=1
        )

        response = self.client.get(
            reverse('report_stages'), {'q': str(order.id)}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['report_data'][0]['representative'],
            'representative-user',
        )


class CustomPaintCardTests(TestCase):
    """کارت‌های دلخواه نقاشی بدون سفارش (order=None)"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('customcard', password='testpass')

    def setUp(self):
        self.client.login(username='customcard', password='testpass')

    def test_create_custom_card_does_not_create_order(self):
        before = Order.objects.count()
        response = self.client.post(
            reverse('painting_create_custom_task'),
            {'title': 'تمیزکاری دستگاه', 'duration_minutes': '30'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(Order.objects.count(), before)
        task = ProductionTask.objects.get(custom_title='تمیزکاری دستگاه')
        self.assertIsNone(task.order_id)
        self.assertIn('دلخواه', str(task))

    def test_completing_custom_card_is_safe(self):
        task = ProductionTask.objects.create(
            order=None, station_name='paint', step_order=1, quantity=1,
            status='pending', custom_title='کار تست',
        )
        task.status = 'done'
        task.save()
        task.refresh_from_db()
        self.assertEqual(task.status, 'done')
        self.assertFalse(ProductionEvent.objects.filter(task=task).exists())

    def test_admin_delete_custom_card_redirects_to_task_list(self):
        task = ProductionTask.objects.create(
            order=None, station_name='paint', step_order=1, quantity=1,
            status='pending', custom_title='حذفی',
        )
        response = self.client.post(reverse('admin_delete_task', args=[task.id]))
        self.assertRedirects(
            response, reverse('admin_tasks_management'), fetch_redirect_response=False
        )
        self.assertFalse(ProductionTask.objects.filter(pk=task.id).exists())
