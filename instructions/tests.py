"""دستورالعمل‌های تولید: منطق تطبیق شرط‌ها، سریال‌سازی برای صفحه اسکن و سند محصول."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from instructions.models import WorkInstruction
from instructions.utils import collect_item_instructions
from product.models import (
    Color,
    ColorCode,
    Customer,
    Material,
    Order,
    OrderItem,
    Part,
    Product,
    ProductCategory,
    ProductionTask,
    WorkerProfile,
)


def _base_data(cls):
    cls.category = ProductCategory.objects.create(name='میزتلویزیون')
    cls.product = Product.objects.create(
        category=cls.category, name='میز فیدان', base_price=1000,
    )
    cls.mdf_raw = Material.objects.create(name='MDF خام', thickness=16)
    cls.mdf_veneer = Material.objects.create(name='MDF روکش بلوط', thickness=16)
    cls.color13 = ColorCode.objects.create(code='13')
    cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
    cls.user = User.objects.get_or_create(username='szadmin', is_superuser=True)[0]
    cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='SZ-1')


class MatchingTests(TestCase):
    """فیلتر applicable_to: ایستگاه، شرط متریال، شرط رنگ و AND بین شرط‌ها."""

    @classmethod
    def setUpTestData(cls):
        _base_data(cls)
        cls.ins_pvc = WorkInstruction.objects.create(
            product=cls.product, station='pvc', kind=WorkInstruction.KIND_NOTE,
            title='سه ضلع روکش طبیعی + ضلع پشت PVC',
        )
        cls.ins_raw = WorkInstruction.objects.create(
            product=cls.product, station='pvc', kind=WorkInstruction.KIND_WARNING,
            title='اگر MDF خام باشد نوار نمی‌خورد', condition_material=cls.mdf_raw,
        )
        cls.ins_color = WorkInstruction.objects.create(
            product=cls.product, station='mon', kind=WorkInstruction.KIND_NOTE,
            title='برای رنگ ۱۳ چسب مخصوص', condition_color=cls.color13,
        )
        cls.ins_general = WorkInstruction.objects.create(
            product=cls.product, station=WorkInstruction.GENERAL, kind=WorkInstruction.KIND_NOTE,
            title='عمومی همه ایستگاه‌ها',
        )
        cls.ins_and = WorkInstruction.objects.create(
            product=cls.product, station='pvc', kind=WorkInstruction.KIND_NOTE,
            title='شرط ترکیبی', condition_material=cls.mdf_raw, condition_color=cls.color13,
        )
        cls.ins_inactive = WorkInstruction.objects.create(
            product=cls.product, station='pvc', title='غیرفعال', active=False,
        )

    def _titles(self, qs):
        return {ins.title for ins in qs}

    def test_station_filter_includes_general_and_excludes_other_stations(self):
        got = self._titles(WorkInstruction.objects.applicable_to(station='pvc'))
        self.assertIn(self.ins_pvc.title, got)
        self.assertIn(self.ins_general.title, got)
        self.assertNotIn(self.ins_color.title, got)  # ایستگاه mon

    def test_material_condition_matches_only_same_material(self):
        got = self._titles(WorkInstruction.objects.applicable_to(
            material_ids=[self.mdf_raw.id], station='pvc'))
        self.assertIn(self.ins_raw.title, got)
        got = self._titles(WorkInstruction.objects.applicable_to(
            material_ids=[self.mdf_veneer.id], station='pvc'))
        self.assertNotIn(self.ins_raw.title, got)
        self.assertIn(self.ins_pvc.title, got)  # بدون شرط همیشه applicable

    def test_unknown_materials_block_conditioned_instructions(self):
        """وقتی متریال سفارش شناخته نیست، دستورالعمل شرط‌دار fail-closed حذف می‌شود."""
        got = self._titles(WorkInstruction.objects.applicable_to(station='pvc'))
        self.assertNotIn(self.ins_raw.title, got)

    def test_color_condition_matches_by_code(self):
        got = self._titles(WorkInstruction.objects.applicable_to(color_codes=['13'], station='mon'))
        self.assertIn(self.ins_color.title, got)
        got = self._titles(WorkInstruction.objects.applicable_to(color_codes=['15'], station='mon'))
        self.assertNotIn(self.ins_color.title, got)

    def test_conditions_are_anded(self):
        got = self._titles(WorkInstruction.objects.applicable_to(
            material_ids=[self.mdf_raw.id], color_codes=['13'], station='pvc'))
        self.assertIn(self.ins_and.title, got)
        got = self._titles(WorkInstruction.objects.applicable_to(
            material_ids=[self.mdf_raw.id], color_codes=['15'], station='pvc'))
        self.assertNotIn(self.ins_and.title, got)

    def test_inactive_excluded(self):
        got = self._titles(WorkInstruction.objects.active().applicable_to(station='pvc'))
        self.assertNotIn(self.ins_inactive.title, got)

    def test_clean_requires_product_or_part(self):
        ins = WorkInstruction(station='cnc', title='بی‌مقصد')
        with self.assertRaises(Exception):
            ins.full_clean()


class CollectItemTests(TestCase):
    """گردآوری دستورالعمل برای آیتم واقعی: سطح محصول + سطح قطعه + شرط متریال."""

    @classmethod
    def setUpTestData(cls):
        _base_data(cls)
        cls.part_page = Part.objects.create(  # صفحه شش‌ضلعی با متریال روکش‌دار
            material=cls.mdf_veneer, name='صفحه شش‌ضلعی', pname=cls.product.name,
            length=600, width=400, routing_code='cnc',
        )
        cls.part_back = Part.objects.create(
            material=cls.mdf_raw, name='پشت‌بند', pname=cls.product.name,
            length=600, width=150, routing_code='dr',
        )
        cls.item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=1,
        )
        Color.objects.create(orderitem=cls.item, part='بدنه', code='13')
        cls.task_page = ProductionTask.objects.create(
            order=cls.order, part=cls.part_page, order_item=cls.item,
            station_name='cnc', step_order=1, quantity=1, status='pending',
        )
        cls.task_back = ProductionTask.objects.create(
            order=cls.order, part=cls.part_back, order_item=cls.item,
            station_name='cnc', step_order=2, quantity=1, status='pending',
        )
        cls.ins_product = WorkInstruction.objects.create(
            product=cls.product, station='cnc', kind=WorkInstruction.KIND_STEP,
            title='مرحله محصول', body='خط اول\nخط دوم',
        )
        cls.ins_part = WorkInstruction.objects.create(
            part=cls.part_page, station='cnc', kind=WorkInstruction.KIND_WARNING,
            title='جهت قرارگیری صفحه', body='قطعه با رگه رو به بالا',
        )
        cls.ins_raw_cond = WorkInstruction.objects.create(
            product=cls.product, station='cnc', title='فقط MDF خام',
            condition_material=cls.mdf_raw,
        )

    def test_includes_product_and_part_levels_with_material_condition(self):
        data = collect_item_instructions(self.item, 'cnc')
        titles = {ins['title'] for ins in data}
        self.assertIn(self.ins_product.title, titles)
        self.assertIn(self.ins_part.title, titles)
        self.assertIn(self.ins_raw_cond.title, titles)  # متریال part_back = MDF خام

    def test_part_level_only_for_parts_of_this_item(self):
        other_part = Part.objects.create(
            material=self.mdf_raw, name='قطعه سفارش دیگر', pname='x',
            length=10, width=10, routing_code='cnc',
        )
        WorkInstruction.objects.create(
            part=other_part, station='cnc', title='دستورالعمل قطعه بیگانه',
        )
        titles = {ins['title'] for ins in collect_item_instructions(self.item, 'cnc')}
        self.assertNotIn('دستورالعمل قطعه بیگانه', titles)

    def test_serialization_shape(self):
        data = collect_item_instructions(self.item, 'cnc')
        ins = next(x for x in data if x['title'] == self.ins_part.title)
        self.assertEqual(ins['kind'], 'warning')
        self.assertEqual(ins['badge'], ['هشدار', 'danger'])
        self.assertEqual(ins['body_lines'], ['قطعه با رگه رو به بالا'])
        self.assertEqual(ins['part_name'], 'صفحه شش‌ضلعی')
        self.assertEqual(ins['images'], [])

    def test_scan_item_tasks_endpoint_returns_instructions(self):
        WorkerProfile.objects.create(user=self.user, stage='cnc')
        self.client.force_login(self.user)
        resp = self.client.get(
            reverse('scan_item_tasks_ajax', args=[self.item.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload['success'])
        titles = {ins['title'] for ins in payload['instructions']}
        self.assertIn(self.ins_product.title, titles)


class ProductDocTests(TestCase):
    """سند دستور تولید محصول: دسترسی و گروه‌بندی ایستگاه‌ها به ترتیب مسیر تولید."""

    @classmethod
    def setUpTestData(cls):
        _base_data(cls)
        cls.staff = User.objects.create_user('staffer', password='x', is_staff=True)
        cls.ins_cnc = WorkInstruction.objects.create(
            product=cls.product, station='cnc', kind=WorkInstruction.KIND_STEP,
            title='مرحله CNC',
        )
        cls.ins_general = WorkInstruction.objects.create(
            product=cls.product, station=WorkInstruction.GENERAL,
            kind=WorkInstruction.KIND_WARNING, title='هشدار عمومی',
        )

    def test_anonymous_redirected(self):
        url = reverse('instructions:product_doc', args=[self.product.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)

    def test_regular_logged_in_user_denied(self):
        User.objects.create_user('plain', password='x')
        self.client.login(username='plain', password='x')
        url = reverse('instructions:product_doc', args=[self.product.id])
        self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_staff_sees_grouped_sections_in_order(self):
        self.client.force_login(self.staff)
        url = reverse('instructions:product_doc', args=[self.product.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('مرحله CNC', body)
        self.assertIn('هشدار عمومی', body)
        # ترتیب: عمومی قبل از CNC (ابتدای STATION_ORDER)
        self.assertLess(body.index('هشدار عمومی'), body.index('مرحله CNC'))

    def test_doc_shows_condition_labels(self):
        WorkInstruction.objects.create(
            product=self.product, station='pvc', title='نوار نمی‌خورد',
            condition_material=self.mdf_raw,
        )
        self.client.force_login(self.staff)
        body = self.client.get(
            reverse('instructions:product_doc', args=[self.product.id])
        ).content.decode()
        self.assertIn('فقط متریال: MDF خام', body)
