"""تست‌های E1: لینک چندگانهٔ حرکت انبار به درخواست مواد (FK به‌جای OneToOne)."""
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from inventory import services
from inventory.models import (
    MaterialIssue,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)
from product.models import (
    Color,
    Customer,
    Order,
    OrderItem,
    PaintingProcess,
    PaintingStage,
    Product,
    ProductCategory,
    ProductionDefect,
    ProductionTask,
)


class MultiMovementIssueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('e1user', password='testpass')
        cls.cat = RawMaterialCategory.objects.create(name='مواد اولیه E1')
        cls.raw = RawMaterial.objects.create(
            category=cls.cat, name='ماده E1', unit='lit', pack_size=0,
        )
        cls.other_raw = RawMaterial.objects.create(
            category=cls.cat, name='ماده دیگر E1', unit='lit', pack_size=0,
        )
        StockMovement.objects.create(
            raw_material=cls.raw, movement_type='purchase',
            quantity=Decimal('100'), note='موجودی اولیه',
        )
        StockMovement.objects.create(
            raw_material=cls.other_raw, movement_type='purchase',
            quantity=Decimal('100'), note='موجودی اولیه',
        )

        cls.product_category = ProductCategory.objects.create(name='دسته E1')
        cls.product = Product.objects.create(
            category=cls.product_category, name='محصول E1', base_price=1000,
        )
        cls.customer = Customer.objects.create(name='مشتری E1', phone='09120000000')
        cls.order = Order.objects.create(user=cls.user, customer=cls.customer, number='E1')
        cls.order_item = OrderItem.objects.create(
            order=cls.order, product=cls.product, quantity=2,
        )
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        cls.process = PaintingProcess.objects.create(
            name='روند E1', code='E1', color_codes=['8'], is_active=True,
        )
        cls.stage = PaintingStage.objects.create(
            process=cls.process, order=1, name='مرحله E1',
            duration_minutes=10, drying_time_minutes=0, required_skill='painter',
        )
        cls.task = ProductionTask.objects.create(
            order=cls.order, part=None, station_name='paint', step_order=1,
            quantity=2, status='pending', painting_stage=cls.stage,
            order_item=cls.order_item, color_part='بدنه',
        )

    def setUp(self):
        self.client.login(username='e1user', password='testpass')

    def _issue(self, purpose='production', requested=Decimal('5'), with_defect=False):
        defect = None
        if with_defect:
            defect = ProductionDefect.objects.create(
                task=self.task, order=self.order, order_item=self.order_item,
                color_part='بدنه', quantity=1, description='خرابی E1',
                reported_by=self.user,
            )
        return MaterialIssue.objects.create(
            task=self.task, defect=defect, raw_material=self.raw,
            requested_quantity=requested, purpose=purpose,
            status='requested', requested_by=self.user,
        )

    def test_two_partial_handovers_create_two_linked_movements(self):
        issue = self._issue(requested=Decimal('5'))

        services.execute_handover(
            issued_by=self.user, items=[(issue.id, Decimal('2'))], note='مرحله اول',
        )
        services.execute_handover(
            issued_by=self.user, items=[(issue.id, Decimal('3'))], note='مرحله دوم',
        )

        issue.refresh_from_db()
        movements = issue.movements.order_by('pk')
        self.assertEqual(movements.count(), 2)
        self.assertEqual(
            sum((m.quantity for m in movements), Decimal('0')), Decimal('5'),
        )
        self.assertEqual(issue.issued_quantity, Decimal('5'))
        self.assertEqual(issue.status, 'issued')
        for movement in movements:
            self.assertEqual(movement.fulfilled_issue_id, issue.id)
            self.assertEqual(movement.reference_task_id, self.task.id)

    def test_rework_two_partial_handovers_counted_as_defect_material(self):
        issue = self._issue(purpose='rework', requested=Decimal('5'), with_defect=True)

        services.execute_handover(
            issued_by=self.user, items=[(issue.id, Decimal('2'))], note='ناقص اول',
        )
        services.execute_handover(
            issued_by=self.user, items=[(issue.id, Decimal('3'))], note='ناقص دوم',
        )

        issue.refresh_from_db()
        self.assertEqual(issue.movements.count(), 2)
        self.assertEqual(issue.status, 'issued')

        response = self.client.get(reverse('report_material_consumption'))
        self.assertEqual(response.status_code, 200)
        rows = response.context['report_rows']
        self.assertEqual(len(rows), 1)
        # هیچ‌کدام از حرکت‌های خرابی نباید در «مصرف تولید عادی» شمرده شود
        self.assertNotIn(self.raw.id, rows[0]['materials'])
        # هر دو تحویل باید زیر «مواد مصرف خرابی» جمع شوند
        self.assertEqual(
            rows[0]['defect_materials'][self.raw.id]['qty'], Decimal('5'),
        )
        defect_totals = response.context['defect_material_totals']
        self.assertEqual(len(defect_totals), 1)
        self.assertEqual(defect_totals[0]['qty'], Decimal('5'))

    def test_production_two_partial_handovers_counted_as_normal_material(self):
        issue = self._issue(purpose='production', requested=Decimal('5'))

        services.execute_handover(
            issued_by=self.user, items=[(issue.id, Decimal('2'))], note='ناقص اول',
        )
        services.execute_handover(
            issued_by=self.user, items=[(issue.id, Decimal('3'))], note='ناقص دوم',
        )

        issue.refresh_from_db()
        self.assertEqual(issue.movements.count(), 2)

        response = self.client.get(reverse('report_material_consumption'))
        self.assertEqual(response.status_code, 200)
        rows = response.context['report_rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['materials'][self.raw.id]['qty'], Decimal('5'))
        self.assertNotIn(self.raw.id, rows[0]['defect_materials'])

    def test_backfill_regex_respects_id_boundary(self):
        short = MaterialIssue.objects.create(
            pk=5, task=self.task, raw_material=self.raw,
            requested_quantity=Decimal('1'), issued_quantity=Decimal('1'),
            purpose='rework', status='issued', requested_by=self.user,
        )
        long_issue = MaterialIssue.objects.create(
            pk=50, task=self.task, raw_material=self.other_raw,
            requested_quantity=Decimal('1'), issued_quantity=Decimal('1'),
            purpose='rework', status='issued', requested_by=self.user,
        )
        movement_five = StockMovement.objects.create(
            raw_material=self.raw, movement_type='consumption', quantity=Decimal('1'),
            reference_task=self.task, note='تحویل انبار #5 — جبران خرابی / ساخت مجدد',
        )
        movement_fifty = StockMovement.objects.create(
            raw_material=self.raw, movement_type='consumption', quantity=Decimal('1'),
            reference_task=self.task, note='تحویل انبار #50 — جبران خرابی / ساخت مجدد',
        )

        out = StringIO()
        call_command('backfill_material_issue_links', stdout=out)

        movement_five.refresh_from_db()
        movement_fifty.refresh_from_db()
        self.assertEqual(movement_five.fulfilled_issue_id, short.id)
        self.assertIsNone(movement_fifty.fulfilled_issue_id)
        self.assertEqual(long_issue.movements.count(), 0)
        self.assertIn('linked:', out.getvalue())

    def test_makemigrations_check_is_clean(self):
        out = StringIO()
        try:
            call_command(
                'makemigrations', 'inventory', check=True, dry_run=True,
                stdout=out, verbosity=1,
            )
        except SystemExit:
            self.fail('makemigrations found changes: ' + out.getvalue())
