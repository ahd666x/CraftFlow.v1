"""UI/UX unification tests (Task 67): Selvi file naming, Trim/ضایعات wording,
material name without thickness, category-first product display, rep visibility.

Every download assertion here uses the real endpoint and real DB rows — no
mocked filenames.
"""
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from product.models import Customer, Material, Order, Part, ProductionTask

from .filenames import (
    order_token, output_filename, program_token, sanitize_token,
)
from .models import CutPlan


class FilenameHelperTests(TestCase):
    def test_pattern_single_order(self):
        self.assertEqual(
            output_filename(['386'], 1, 'CUT', 'CUT'),
            'Selvi_Order-386_Program-01_CUT.CUT',
        )

    def test_pattern_multi_order_and_nc(self):
        self.assertEqual(
            output_filename(['386', '387'], 2, 'CNC', 'nc'),
            'Selvi_Order-386-387_Program-02_CNC.nc',
        )

    def test_invalid_filename_characters_sanitized(self):
        self.assertEqual(sanitize_token('MDF/بد:1*2?'), 'MDF-بد-1-2-')
        self.assertEqual(order_token(['a/b', 'c', 'a/b']), 'a-b-c')
        self.assertEqual(order_token([]), 'NA')

    def test_program_token_zero_pads_real_numbers_only(self):
        self.assertEqual(program_token(1), '01')
        self.assertEqual(program_token(137), '137')
        self.assertEqual(program_token('x'), 'x')


class CutDownloadFilenameTests(TestCase):
    """نام فایل خروجی CUT با شماره سفارش و شماره برنامهٔ واقعی ساخته می‌شود."""

    def setUp(self):
        self.user = User.objects.create_superuser(username='cut-admin', password='pw12345')
        self.customer = Customer.objects.create(name='مشتری نام‌گذاری')
        self.material = Material.objects.create(name='MDF سفید', thickness=Decimal('16.0'))
        self.order = Order.objects.create(customer=self.customer, number='386', status='planned')
        self.client.force_login(self.user)

    def _plan(self, *orders):
        plan = CutPlan.objects.create(status='planned', plan_data={})
        plan.orders.set(orders)
        return plan

    def test_single_order_uses_real_order_number_and_sequence(self):
        first = self._plan(self.order)
        response = self.client.get(reverse('cutting:download_cut', args=[first.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Disposition'],
            'attachment; filename="Selvi_Order-386_Program-01_CUT.CUT"',
        )
        second = self._plan(self.order)
        self.assertEqual(
            self.client.get(reverse('cutting:download_cut', args=[second.pk]))['Content-Disposition'],
            'attachment; filename="Selvi_Order-386_Program-02_CUT.CUT"',
        )

    def test_multi_order_plan_joins_real_numbers_and_keeps_global_program_id(self):
        other = Order.objects.create(customer=self.customer, number='387', status='planned')
        plan = self._plan(self.order, other)
        response = self.client.get(reverse('cutting:download_cut', args=[plan.pk]))
        self.assertEqual(
            response['Content-Disposition'],
            f'attachment; filename="Selvi_Order-386-387_Program-{plan.pk:02d}_CUT.CUT"',
        )

    def test_blank_order_number_falls_back_to_order_pk(self):
        self.order.number = ''
        self.order.save(update_fields=('number',))
        plan = self._plan(self.order)
        response = self.client.get(reverse('cutting:download_cut', args=[plan.pk]))
        self.assertEqual(
            response['Content-Disposition'],
            f'attachment; filename="Selvi_Order-{self.order.pk}_Program-01_CUT.CUT"',
        )

    def test_content_format_unchanged(self):
        import xml.etree.ElementTree as ET
        plan = self._plan(self.order)
        response = self.client.get(reverse('cutting:download_cut', args=[plan.pk]))
        self.assertEqual(ET.fromstring(response.content).tag, '{http://www.King-stone.com}AutoCUT')


class CncDownloadFilenameTests(TestCase):
    """نام فایل خروجی CNC در همهٔ مسیرها یکسان است (دانلود مستقیم و JSON)."""

    def setUp(self):
        from cnc_resizer.models import CncSheetPlan
        from cutting.models import SheetStock
        self.user = User.objects.create_superuser(username='cnc-admin', password='pw12345')
        self.customer = Customer.objects.create(name='مشتری CNC')
        self.material = Material.objects.create(name='MDF طرح چوب', thickness=Decimal('25.0'))
        self.order = Order.objects.create(customer=self.customer, number='386', status='planned')
        self.stock = SheetStock.objects.create(
            material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'), quantity=1,
        )
        self.plan = CncSheetPlan.objects.create(
            material=self.material, source_stock=self.stock, width=Decimal('1830.0'),
            height=Decimal('3660.0'), status='done', output_verified=True,
            gcode='G90\nM141\nM30\n', layout={'parts': [{'task_id': None, 'order_id': self.order.pk}]},
            created_by=self.user,
        )
        self.client.force_login(self.user)

    def test_direct_download_uses_selvi_pattern(self):
        response = self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[self.plan.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Disposition'],
            'attachment; filename="Selvi_Order-386_Program-01_CNC.nc"',
        )
        # محتوای واقعی فایل دست‌نخورده می‌ماند
        self.assertIn(b'G90', response.content)

    def test_sequence_advances_for_next_program_of_same_order(self):
        from cnc_resizer.models import CncSheetPlan
        second = CncSheetPlan.objects.create(
            material=self.material, source_stock=self.stock, sheet_index=1, width=Decimal('1830.0'),
            height=Decimal('3660.0'), status='done', output_verified=True,
            gcode='G90\nM141\nM30\n', layout={'parts': [{'task_id': None, 'order_id': self.order.pk}]},
            created_by=self.user,
        )
        response = self.client.get(reverse('cnc_resizer:download_sheet_plan', args=[second.pk]))
        self.assertEqual(
            response['Content-Disposition'],
            'attachment; filename="Selvi_Order-386_Program-02_CNC.nc"',
        )


class WorkspaceCategoryAndSpecApiTests(TestCase):
    """API فضای کاری دستهٔ واقعی محصول و نام متریال بدون ضخامت می‌دهد."""

    def setUp(self):
        self.user = User.objects.create_superuser(username='ui-admin', password='pw12345')
        self.customer = Customer.objects.create(name='مشتری دسته')
        self.material = Material.objects.create(name='MDF سفید', thickness=Decimal('16.0'))
        self.part = Part.objects.create(
            material=self.material, name='دیواره', length=Decimal('400.0'), width=Decimal('300.0'),
            pname='فیدان', routing_code='cut',
        )
        self.order = Order.objects.create(customer=self.customer, number='201', status='planned')
        self.task = ProductionTask.objects.create(
            order=self.order, part=self.part, station_name='cut', step_order=1,
            quantity=2, status='pending',
        )
        self.client.force_login(self.user)

    def _seed_item(self):
        from product.models import OrderItem, Product, ProductCategory
        category = ProductCategory.objects.create(name='جلومبلی')
        product = Product.objects.create(name='فیدان', category=category)
        item = OrderItem.objects.create(order=self.order, product=product, quantity=2, size='400x300')
        self.task.order_item = item
        self.task.save(update_fields=('order_item',))
        return item

    def test_parts_rows_carry_real_category_for_category_first_display(self):
        item = self._seed_item()
        payload = self.client.get(reverse('cutting:ws_parts', args=[self.order.pk])).json()
        row = payload['rows'][0]
        self.assertEqual(row['category_name'], 'جلومبلی')
        self.assertEqual(row['pname'], 'فیدان')
        self.assertEqual(row['item_id'], item.pk)

    def test_parts_rows_without_item_have_empty_category(self):
        payload = self.client.get(reverse('cutting:ws_parts', args=[self.order.pk])).json()
        self.assertEqual(payload['rows'][0]['category_name'], '')

    def test_order_items_carry_category_too(self):
        self._seed_item()
        payload = self.client.get(reverse('cutting:ws_order', args=[self.order.pk])).json()
        self.assertEqual(payload['items'][0]['category_name'], 'جلومبلی')

    def test_material_payload_keeps_thickness_as_data_not_display(self):
        payload = self.client.get(reverse('cutting:ws_order', args=[self.order.pk])).json()
        self.assertEqual(payload['materials'][0]['name'], 'MDF سفید')  # بدون ضخامت


class StationWordingTests(TestCase):
    """Trim جایگزین سربر؛ ضایعات جایگزین اسقاط؛ برند Selvi در صفحات دو اپ."""

    def setUp(self):
        self.user = User.objects.create_superuser(username='word-admin', password='pw12345')
        self.customer = Customer.objects.create(name='مشتری واژگان')
        self.material = Material.objects.create(name='MDF سفید', thickness=Decimal('16.0'))
        self.client.force_login(self.user)

    def test_planner_shows_trim_and_never_sarbar(self):
        response = self.client.get(reverse('cutting:planner'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Trim (mm)')
        self.assertNotContains(response, 'سربر')

    def test_inventory_uses_zayeat_not_esqat(self):
        response = self.client.get(reverse('cutting:inventory'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ضایعات')
        self.assertNotContains(response, 'اسقاط')

    def test_history_page_uses_zayeat(self):
        response = self.client.get(reverse('cutting:history'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ضایعات')
        self.assertNotContains(response, 'اسقاط')

    def test_stock_form_material_options_have_no_thickness(self):
        response = self.client.get(reverse('cutting:stock_create'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ضخامت 16.0 mm')  # گروه‌بندی
        self.assertNotContains(response, 'MDF سفید — 16.0 mm')  # دیگر در نام نیست

    def test_sheet_plans_page_renders(self):
        response = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertEqual(response.status_code, 200)
