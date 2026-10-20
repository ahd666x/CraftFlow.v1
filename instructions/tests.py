"""دستورالعمل‌های تولید: منطق تطبیق شرط‌ها، سریال‌سازی برای صفحه اسکن و سند محصول."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from instructions.cnc_guide import (
    assess_points,
    cut_bounds_from_text,
    get_active_setup,
    part_cnc_guide,
    serialize_setup,
)
from instructions.models import (
    CncFixturePoint,
    CncMachineSetup,
    CncToolSlot,
    WorkInstruction,
)
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


# G-code حداقلی برای تحلیل کادر برش: مستطیل 100×50 از مبدأ برنامه
MINIMAL_GCODE = """%
N10 G90G54
N20 G21
N30 T1M6
N40 G43H1
N50 G0X0Y0
N60 G1X100Y0F6000
N70 G1X100Y50
N80 G1X0Y50
N90 G1X0Y0
N100 G0Z5
N110 G53Z0
N120 M30
%
"""


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


class CncGuideEngineTests(TestCase):
    """موتور راهنمای خودکار CNC: سریال‌سازی، کادر برش و ارزیابی ممنوع/امن."""

    @classmethod
    def setUpTestData(cls):
        _base_data(cls)
        cls.part = Part.objects.create(
            material=cls.mdf_veneer, name='قطعه برش CNC', pname=cls.product.name,
            length=600, width=400, routing_code='cnc',
        )
        cls.setup = CncMachineSetup.objects.create(
            name='CNC اصلی',
            table_width=2100, table_height=3200,
            origin_x=150, origin_y=100,
            safety_margin=10,
            offset_procedure='قطعه را به پین توقف عقب بچسبانید\nZ تاج را روی سطح ورق سِت کنید',
            notes='قبل از شروع وکیوم را چک کنید',
        )
        CncToolSlot.objects.create(setup=cls.setup, slot=1, cutter_type=CncToolSlot.CUTTER_COMPRESSION, diameter=8, description='برش ۱۶ میلی')
        CncToolSlot.objects.create(setup=cls.setup, slot=2, cutter_type=CncToolSlot.CUTTER_FLAT, diameter=6, description='شیار و سوراخ')
        # M1: روی مسیر برش (مختصات برنامه 50,25) → ممنوع
        cls.p_inside = CncFixturePoint.objects.create(setup=cls.setup, label='M1', kind=CncFixturePoint.KIND_SCREW, x=200, y=125)
        # M2: بیرون قطعه (مختصات برنامه 1500,2600) → امن
        cls.p_safe = CncFixturePoint.objects.create(setup=cls.setup, label='M2', kind=CncFixturePoint.KIND_CLAMP, x=1650, y=2700)
        # M3: نزدیک لبه — با شعاع ممنوع 20 داخل ناحیه ممنوع می‌افتد (برنامه X=115 یعنی 15mm بیرون لبه 100)
        cls.p_edge = CncFixturePoint.objects.create(setup=cls.setup, label='M3', kind=CncFixturePoint.KIND_STOP, x=265, y=150, radius=20)
        cls.p_inactive = CncFixturePoint.objects.create(setup=cls.setup, label='M9', x=200, y=125, active=False)
        cls.setup_inactive = CncMachineSetup.objects.create(
            name='ماشین غیرفعال', table_width=1000, table_height=1000, origin_x=0, origin_y=0, active=False,
        )

    def test_get_active_setup_returns_only_active(self):
        self.assertEqual(get_active_setup().pk, self.setup.pk)
        self.setup.active = False
        self.setup.save(update_fields=('active',))
        try:
            self.assertIsNone(get_active_setup())
        finally:
            self.setup.active = True
            self.setup.save(update_fields=('active',))

    def test_serialize_setup_shape(self):
        data = serialize_setup(self.setup)
        self.assertEqual(data['name'], 'CNC اصلی')
        self.assertEqual(data['table'], {'width': 2100, 'height': 3200})
        self.assertEqual(data['origin'], {'x': 150, 'y': 100})
        self.assertEqual([t['slot'] for t in data['tools']], [1, 2])
        self.assertEqual(data['tools'][0]['label'], 'T1')
        self.assertEqual(data['tools'][0]['diameter_display'], '8')
        self.assertEqual(data['offset_procedure'], ['قطعه را به پین توقف عقب بچسبانید', 'Z تاج را روی سطح ورق سِت کنید'])
        self.assertEqual(data['notes'], ['قبل از شروع وکیوم را چک کنید'])
        # نقطه غیرفعال سریال نمی‌شود
        self.assertEqual([p['label'] for p in data['points']], ['M1', 'M2', 'M3'])
        self.assertIsNone(data['points'][0]['status'])

    def test_unique_tool_slot_per_setup(self):
        duplicate = CncToolSlot(setup=self.setup, slot=1, cutter_type=CncToolSlot.CUTTER_FLAT)
        with self.assertRaises(Exception):
            duplicate.full_clean()

    def test_cut_bounds_from_minimal_gcode(self):
        bounds = cut_bounds_from_text(MINIMAL_GCODE)
        self.assertEqual(bounds, {'x0': 0.0, 'y0': 0.0, 'x1': 100.0, 'y1': 50.0})
        self.assertIsNone(cut_bounds_from_text('این گicode نیست'))

    def test_assess_points_forbidden_safe_and_none(self):
        bounds = {'x0': 0, 'y0': 0, 'x1': 100, 'y1': 50}
        statuses = {p['label']: p['status'] for p in assess_points(self.setup, bounds)}
        self.assertEqual(statuses['M1'], 'forbidden')  # داخل مسیر برش
        self.assertEqual(statuses['M2'], 'safe')       # خیلی بیرون
        self.assertEqual(statuses['M3'], 'forbidden')  # 15mm بیرون لبه ولی شعاع ممنوع 20 + حاشیه 10
        # بدون کادر برش هیچ ارزیابی‌ای انجام نمی‌شود (fail-open به «بدون وضعیت»)
        for p in assess_points(self.setup, None):
            self.assertIsNone(p['status'])

    def test_part_cnc_guide_without_active_setup_is_none(self):
        self.setup.active = False
        self.setup.save(update_fields=('active',))
        try:
            self.assertIsNone(part_cnc_guide(self.part))
        finally:
            self.setup.active = True
            self.setup.save(update_fields=('active',))

    def test_part_cnc_guide_assesses_with_real_file(self):
        class FakeRecord:
            path = None
        with mock.patch('cnc_resizer.services.resolve_cnc_file', return_value=FakeRecord()) as _:
            import tempfile, os
            with tempfile.NamedTemporaryFile('w', suffix='.cnc', delete=False, encoding='utf-8') as fh:
                fh.write(MINIMAL_GCODE)
                FakeRecord.path = fh.name
            try:
                payload = part_cnc_guide(self.part, self.user)
            finally:
                os.unlink(FakeRecord.path)
        self.assertTrue(payload['part_assessed'])
        statuses = {p['label']: p['status'] for p in payload['points']}
        self.assertEqual(statuses['M1'], 'forbidden')
        self.assertEqual(statuses['M2'], 'safe')

    def test_part_cnc_guide_degrades_to_unassessed_when_file_missing(self):
        with mock.patch('cnc_resizer.services.resolve_cnc_file', side_effect=OSError('no file')):
            payload = part_cnc_guide(self.part, self.user)
        self.assertFalse(payload['part_assessed'])
        self.assertIsNone(payload['points'][0]['status'])


class CncGuideIntegrationTests(TestCase):
    """اتصال راهنمای CNC به صفحه اسکن و سند دستور تولید."""

    @classmethod
    def setUpTestData(cls):
        _base_data(cls)
        cls.staff = User.objects.create_user('staffer2', password='x', is_staff=True)
        cls.setup = CncMachineSetup.objects.create(
            name='CNC کارگاه', table_width=2100, table_height=3200, origin_x=150, origin_y=100,
        )
        CncToolSlot.objects.create(setup=cls.setup, slot=1, cutter_type=CncToolSlot.CUTTER_COMPRESSION, diameter=8)
        CncFixturePoint.objects.create(setup=cls.setup, label='M1', x=200, y=125)
        cls.part = Part.objects.create(
            material=cls.mdf_veneer, name='قطعه اسکن', pname=cls.product.name,
            length=600, width=400, routing_code='cnc',
        )
        cls.item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=1)
        cls.task = ProductionTask.objects.create(
            order=cls.order, part=cls.part, order_item=cls.item,
            station_name='cnc', step_order=1, quantity=1, status='pending',
        )

    def test_scan_item_tasks_returns_cnc_setup_for_cnc_stage(self):
        WorkerProfile.objects.create(user=self.user, stage='cnc')
        self.client.force_login(self.user)
        resp = self.client.get(
            reverse('scan_item_tasks_ajax', args=[self.item.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        payload = resp.json()
        self.assertTrue(payload['success'])
        self.assertIsNotNone(payload['cnc_setup'])
        self.assertEqual(payload['cnc_setup']['name'], 'CNC کارگاه')
        self.assertEqual(payload['cnc_setup']['tools'][0]['label'], 'T1')

    def test_scan_item_tasks_has_no_cnc_setup_for_other_stages(self):
        WorkerProfile.objects.create(user=self.user, stage='pvc')
        self.client.force_login(self.user)
        resp = self.client.get(
            reverse('scan_item_tasks_ajax', args=[self.item.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertIsNone(resp.json()['cnc_setup'])

    def test_cnc_index_embeds_setup_json(self):
        WorkerProfile.objects.create(user=self.user, stage='cnc')
        self.client.force_login(self.user)
        resp = self.client.get(reverse('cnc_resizer:index'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('id="cncSetupData"', body)
        # json_script متن فارسی را escape می‌کند؛ JSON را پارس می‌کنیم
        import json
        import re
        match = re.search(
            r'<script[^>]*id="cncSetupData"[^>]*>(.*?)</script>',
            body, re.S,
        )
        self.assertIsNotNone(match)
        setup = json.loads(match.group(1))
        self.assertEqual(setup['name'], 'CNC کارگاه')
        self.assertEqual(setup['tools'][0]['label'], 'T1')

    def test_product_doc_includes_auto_cnc_section(self):
        """سکشن CNC حتی بدون دستورالعمل دستی هم از راه‌اندازی ماشین ساخته می‌شود."""
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('instructions:product_doc', args=[self.product.id]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('راهنمای خودکار ماشین: CNC کارگاه', body)
        self.assertIn('T1', body)
        self.assertIn('مبدأ G54', body)
