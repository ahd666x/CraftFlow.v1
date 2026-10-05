"""
P17 — گارد مصرف دستی در W12 برای مواد نقاشی.

W12 (`stock_movement_create`) یک فرم عمومی ورود/خروج مواد است که
هیچ ارجاعی به تسک، صف، یا درخواست تحویل ندارد. این تست‌ها تأیید می‌کنند
که مصرف دستی (movement_type='consumption') برای موادی که توسط
صف مواد روزانه نقاشی مدیریت می‌شوند (در PaintingProcessMaterial یا
PaintingColorMaterialVariant) رد می‌شود، در حالی که:

* سایر انواع حرکت (purchase/adjustment/return) مسدود نمی‌شوند
* مصرف مواد غیرنقاشی همچنان مجاز است
* مسیر Engine B (DailyMaterialQueue) تحت تأثیر قرار نمی‌گیرد
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from inventory.models import (
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
    Supplier,
)
from product.models import (
    PaintingColorMaterialVariant,
    PaintingProcess,
    PaintingProcessMaterial,
    ProductCategory,
    Product,
)


class W12PaintConsumptionGuardBase(TestCase):
    """
    بستر اصلی: یک ماده نقاشی و یک ماده غیربنقاشی با دسته‌بندی
    و واحد یکسان، فقط تفاوت در وجود یا عدم وجود در کاتالوگ نقاشی.
    """

    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_superuser('w12mgr', password='pw')

        cls.category = RawMaterialCategory.objects.create(name='مواد W12')
        cls.supplier = Supplier.objects.create(name='تامین‌کننده W12')

        cls.paint_raw = RawMaterial.objects.create(
            category=cls.category, name='رنگ وضعیت W12',
            code='W12-PAINT', unit='kg', pack_size=Decimal('0'),
        )
        cls.regular_raw = RawMaterial.objects.create(
            category=cls.category, name='لوازم CNC W12',
            code='W12-CNC', unit='kg', pack_size=Decimal('0'),
        )

        cls.product_category = ProductCategory.objects.create(name='دسته W12')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول W12', base_price=1000,
        )
        cls.process = PaintingProcess.objects.create(
            name='روند W12', code='W12', color_codes=['8'], is_active=True,
        )

        PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.paint_raw,
            is_color_variant=False,
        )


class W12ConsumptionGuardTests(W12PaintConsumptionGuardBase):
    """Case C — Consumption of paint-managed material is blocked."""

    def _post(self, raw, movement_type, quantity='1', supplier=None):
        self.client.force_login(self.manager)
        data = {
            'raw_material': raw.pk,
            'movement_type': movement_type,
            'quantity': quantity,
            'note': 'W12 test entry',
        }
        if supplier is not None:
            data['supplier'] = supplier
        return self.client.post(
            reverse('inventory:movement_create'),
            data,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def _fund_stock(self, material, quantity=Decimal('100')):
        StockMovement.objects.create(
            raw_material=material, movement_type='purchase',
            quantity=quantity, supplier=self.supplier,
            created_by=self.manager,
        )
        material.refresh_from_db()

    def test_01_consumption_paint_managed_is_blocked(self):
        """Case C: consumption of paint-managed material → BLOCKED."""
        before = StockMovement.objects.count()
        response = self._post(self.paint_raw, 'consumption', '1')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('__all__', data.get('errors', {}))
        self.assertEqual(StockMovement.objects.count(), before,
                         'No StockMovement created for blocked consumption')

    def test_02_consumption_non_paint_is_allowed(self):
        """Case B: consumption of non-paint material → unchanged."""
        self._fund_stock(self.regular_raw)
        before = StockMovement.objects.count()
        response = self._post(self.regular_raw, 'consumption', '1')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(StockMovement.objects.count(), before + 1)
        sm = StockMovement.objects.get(raw_material=self.regular_raw,
                                       movement_type='consumption')
        self.assertEqual(sm.quantity, Decimal('1'))

    def test_03_purchase_paint_managed_is_allowed(self):
        """Case A: purchase of paint-managed material → unchanged (inbound)."""
        before = StockMovement.objects.count()
        response = self._post(self.paint_raw, 'purchase', '5',
                              supplier=self.supplier.pk)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(StockMovement.objects.count(), before + 1)
        sm = StockMovement.objects.get(raw_material=self.paint_raw,
                                       movement_type='purchase')
        self.assertEqual(sm.quantity, Decimal('5'))

    def test_04_adjustment_paint_managed_is_allowed(self):
        """Case A: adjustment of paint-managed material → unchanged."""
        before = StockMovement.objects.count()
        response = self._post(self.paint_raw, 'adjustment', '2')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(StockMovement.objects.count(), before + 1)

    def test_05_return_paint_managed_is_allowed(self):
        """Case A: return of paint-managed material → unchanged."""
        before = StockMovement.objects.count()
        response = self._post(self.paint_raw, 'return', '3')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(StockMovement.objects.count(), before + 1)


class W12ColorVariantGuardTests(W12PaintConsumptionGuardBase):
    """
    A RawMaterial that appears ONLY in PaintingColorMaterialVariant
    (a color-specific actual paint material) must also be blocked.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # یک «اسلات» ماده رنگ‌وابسته و ماده واقعی رنگ خاص
        from inventory.models import RawMaterial as RM
        cls.variant_slot = RM.objects.create(
            category=cls.category, name='اسلات رنگ W12',
            code='W12-SLOT', unit='kg', pack_size=Decimal('0'),
        )
        cls.variant_real = RM.objects.create(
            category=cls.category, name='رنگ قرمز W12',
            code='W12-RED', unit='kg', pack_size=Decimal('0'),
        )
        PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.variant_slot,
            is_color_variant=True,
        )
        PaintingColorMaterialVariant.objects.create(
            process_material=PaintingProcessMaterial.objects.get(
                process=cls.process, raw_material=cls.variant_slot),
            color_code='8',
            raw_material=cls.variant_real,
        )

    def test_01_color_variant_material_consumption_blocked(self):
        """Consumption of color-variant actual material → BLOCKED."""
        before = StockMovement.objects.count()
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse('inventory:movement_create'),
            {
                'raw_material': self.variant_real.pk,
                'movement_type': 'consumption',
                'quantity': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertEqual(StockMovement.objects.count(), before)
