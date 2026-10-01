"""تست‌های مدل «امانت مواد نزد کارگر» (MaterialCustody)."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from inventory import services
from inventory.models import (
    MaterialCustody, MaterialCustodyReturn, MaterialHandover,
    MaterialIssue, MaterialLeftover, RawMaterial, RawMaterialCategory,
    StockMovement,
)
from inventory.services import HandoverError
from product.models import (
    Color, Customer, Order, OrderItem, PaintingProcess, Product, ProductCategory,
    ProductionDefect,
)


class CustodyTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.warehouse = User.objects.create_superuser('cust_wh', password='testpass')
        cls.worker1 = User.objects.create_user('ali', password='testpass', first_name='علی')
        cls.worker2 = User.objects.create_user('reza', password='testpass', first_name='رضا')

        cls.cat = RawMaterialCategory.objects.create(name='مواد نقاشی')
        cls.raw = RawMaterial.objects.create(
            category=cls.cat, name='رنگ', code='PNT', unit='kg', pack_size=Decimal('4.50'),
        )

        cls.pcat = ProductCategory.objects.create(name='دسته')
        cls.product = Product.objects.create(category=cls.pcat, name='محصول', base_price=1000)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')
        cls.order = Order.objects.create(user=cls.warehouse, customer=cls.customer, number='CU1')
        cls.order_item = OrderItem.objects.create(order=cls.order, product=cls.product, quantity=2)
        Color.objects.create(part='بدنه', code='8', orderitem=cls.order_item)
        cls.process = PaintingProcess.objects.create(
            name='روند', code='CU', color_codes=['8'], is_active=True,
        )

    def setUp(self):
        self.client.login(username='cust_wh', password='testpass')
        StockMovement.objects.create(
            raw_material=self.raw, movement_type='purchase', quantity=Decimal('100'),
            note='موجودی اولیه',
        )

    def _issue(self, quantity=Decimal('3')):
        defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, color_part='بدنه',
            quantity=1, description='خرابی', reported_by=self.warehouse,
        )
        return MaterialIssue.objects.create(
            defect=defect, raw_material=self.raw, requested_quantity=quantity,
            purpose='rework', status='requested', requested_by=self.warehouse,
        )


class CustodyHandoverTests(CustodyTestBase):
    def test_handover_creates_custody_for_worker(self):
        issue = self._issue(Decimal('3'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue.pk, Decimal('3'))],
            received_by=self.worker1, held_by=self.worker1,
        )
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        # از بستهٔ ۴.۵ کیلویی ۳ کیلو نیاز بود -> ۱.۵ کیلو نزد نقاش می‌ماند
        self.assertEqual(custody.quantity, Decimal('1.50'))

    def test_two_workers_have_separate_custody(self):
        issue = self._issue(Decimal('3'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue.pk, Decimal('3'))],
            received_by=self.worker1, held_by=self.worker1,
        )
        issue2 = self._issue(Decimal('3'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue2.pk, Decimal('3'))],
            received_by=self.worker2, held_by=self.worker2,
        )
        self.assertEqual(
            MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1).quantity,
            Decimal('1.50'),
        )
        self.assertEqual(
            MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker2).quantity,
            Decimal('1.50'),
        )
        # باقی‌ماندهٔ سالن دست‌نخورده (صفر) می‌ماند
        self.assertFalse(MaterialLeftover.objects.filter(quantity__gt=0).exists())

    def test_second_handover_uses_custody_first(self):
        # نیاز ۲ از بستهٔ ۴.۵ -> ۲.۵ کیلو نزد نقاش می‌ماند
        issue = self._issue(Decimal('2'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue.pk, Decimal('2'))],
            received_by=self.worker1, held_by=self.worker1,
        )
        self.assertEqual(
            MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1).quantity,
            Decimal('2.50'),
        )
        movements_before = StockMovement.objects.count()
        physical_before = self.raw.current_stock

        # نیاز ۲، همه‌اش از امانتِ باز کسر می‌شود -> هیچ بستهٔ جدیدی باز نمی‌شود
        issue2 = self._issue(Decimal('2'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue2.pk, Decimal('2'))],
            received_by=self.worker1, held_by=self.worker1,
        )
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('0.50'))
        self.assertEqual(self.raw.current_stock, physical_before)
        # حرکت مصرفِ خواسته + یک «اصلاحیه» مثبت (چون این ۲ کیلو قبلاً به‌عنوان
        # باقی‌مانده از انبار کسر شده بود) و هیچ حرکت «مصرفِ بستهٔ باز» جدیدی نیست
        self.assertEqual(StockMovement.objects.count(), movements_before + 2)
        self.assertEqual(
            StockMovement.objects.filter(movement_type='consumption', fulfilled_issue=issue2).count(),
            1,
        )

    def test_second_handover_opens_new_pack_when_custody_insufficient(self):
        # ۱.۵ امانت برای نیاز ۲ کافی نیست -> یک بستهٔ ۴.۵ جدید باز می‌شود
        issue = self._issue(Decimal('3'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue.pk, Decimal('3'))],
            received_by=self.worker1, held_by=self.worker1,
        )
        issue2 = self._issue(Decimal('2'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue2.pk, Decimal('2'))],
            received_by=self.worker1, held_by=self.worker1,
        )
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('4.00'))
        self.assertEqual(self.raw.current_stock, Decimal('91.00'))

    def test_legacy_flow_unchanged(self):
        issue = self._issue(Decimal('3'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue.pk, Decimal('3'))],
            received_by=self.worker1,
        )
        leftover = MaterialLeftover.objects.get(raw_material=self.raw)
        self.assertEqual(leftover.quantity, Decimal('1.50'))
        self.assertFalse(MaterialCustody.objects.exists())

    def test_handover_line_records_custody(self):
        issue = self._issue(Decimal('3'))
        handover = services.execute_handover(
            issued_by=self.warehouse, items=[(issue.pk, Decimal('3'))],
            received_by=self.worker1, held_by=self.worker1,
        )
        line = handover.lines.get()
        self.assertEqual(line.leftover_before, Decimal('0.00'))
        self.assertEqual(line.leftover_after, Decimal('1.50'))
        self.assertEqual(line.from_stock_quantity, Decimal('4.50'))
        self.assertEqual(handover.received_by, self.worker1)


class ReturnCustodyTests(CustodyTestBase):
    def _issue_to(self, worker, quantity=Decimal('3')):
        issue = self._issue(quantity)
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue.pk, quantity)],
            received_by=worker, held_by=worker,
        )
        return issue

    def test_return_custody_sets_measured(self):
        self._issue_to(self.worker1)
        record = services.return_custody(
            raw_material=self.raw, held_by=self.worker1, measured_quantity='2.5',
            recorded_by=self.warehouse, note='توزین پایان روز',
        )
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('2.50'))
        self.assertEqual(record.quantity_before, Decimal('1.50'))
        self.assertEqual(record.delta, Decimal('1.00'))
        self.assertEqual(record.note, 'توزین پایان روز')

    def test_return_custody_zeroes_and_logs(self):
        self._issue_to(self.worker1)
        services.return_custody(
            raw_material=self.raw, held_by=self.worker1, measured_quantity='0',
            recorded_by=self.warehouse,
        )
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('0.00'))
        self.assertEqual(
            MaterialCustodyReturn.objects.filter(custody=custody).count(), 1,
        )

    def test_return_custody_creates_row_when_missing(self):
        services.return_custody(
            raw_material=self.raw, held_by=self.worker2, measured_quantity='3',
            recorded_by=self.warehouse,
        )
        self.assertTrue(
            MaterialCustody.objects.filter(raw_material=self.raw, held_by=self.worker2).exists()
        )

    def test_return_custody_creates_no_stock_movement(self):
        self._issue_to(self.worker1)
        before = StockMovement.objects.count()
        services.return_custody(
            raw_material=self.raw, held_by=self.worker1, measured_quantity='2.5',
            recorded_by=self.warehouse,
        )
        self.assertEqual(StockMovement.objects.count(), before)

    def test_return_custody_negative_raises(self):
        self._issue_to(self.worker1)
        with self.assertRaises(HandoverError):
            services.return_custody(
                raw_material=self.raw, held_by=self.worker1, measured_quantity='-1',
                recorded_by=self.warehouse,
            )

    def test_return_custody_invalid_raises(self):
        self._issue_to(self.worker1)
        with self.assertRaises(HandoverError):
            services.return_custody(
                raw_material=self.raw, held_by=self.worker1, measured_quantity='abc',
                recorded_by=self.warehouse,
            )

    def test_return_custody_without_holder_raises(self):
        with self.assertRaises(HandoverError):
            services.return_custody(
                raw_material=self.raw, held_by=None, measured_quantity='1',
                recorded_by=self.warehouse,
            )

    def test_current_stock_matches_physical_out(self):
        """۴.۵ بسته صبح + ۲ مصرف + بازگشت ۲.۵ = موجودی ۹۵.۵"""
        self._issue_to(self.worker1, Decimal('2'))
        services.return_custody(
            raw_material=self.raw, held_by=self.worker1, measured_quantity='2.5',
            recorded_by=self.warehouse,
        )
        self.assertEqual(self.raw.current_stock, Decimal('95.50'))


class PerRowHandoverTests(CustodyTestBase):
    """فرم تحویل تک‌ردیف (دکمهٔ سبز هر ردیف) هم باید امانت بسازد."""

    def test_issue_material_form_creates_custody(self):
        issue = self._issue(Decimal('3'))
        self.client.post(reverse('inventory:issue_material', args=[issue.pk]), {
            'quantity': '3',
            'held_by': self.worker1.pk,
        })
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('1.50'))
        self.assertFalse(MaterialLeftover.objects.filter(quantity__gt=0).exists())
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'issued')
        self.assertEqual(issue.received_by, self.worker1)

    def test_issue_material_form_without_custody_stays_legacy(self):
        issue = self._issue(Decimal('3'))
        self.client.post(reverse('inventory:issue_material', args=[issue.pk]), {
            'quantity': '3',
            'held_by': '',
        })
        self.assertFalse(MaterialCustody.objects.exists())
        self.assertEqual(
            MaterialLeftover.objects.get(raw_material=self.raw).quantity, Decimal('1.50'),
        )
        issue.refresh_from_db()
        self.assertEqual(issue.received_by, self.warehouse)

    def test_issue_material_form_invalid_holder_falls_back(self):
        issue = self._issue(Decimal('3'))
        response = self.client.post(reverse('inventory:issue_material', args=[issue.pk]), {
            'quantity': '3',
            'held_by': 'not-a-number',
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MaterialCustody.objects.exists())
        self.assertEqual(MaterialLeftover.objects.get(raw_material=self.raw).quantity, Decimal('1.50'))


class CustodyViewTests(CustodyTestBase):
    def _seed_custody(self):
        issue = self._issue(Decimal('3'))
        services.execute_handover(
            issued_by=self.warehouse, items=[(issue.pk, Decimal('3'))],
            received_by=self.worker1, held_by=self.worker1,
        )

    def test_custody_return_view_requires_ajax(self):
        self._seed_custody()
        response = self.client.post(reverse('inventory:custody_return'), {
            'raw_material_id': self.raw.id,
            'held_by': self.worker1.id,
            'measured_quantity': '2.5',
        })
        self.assertEqual(response.status_code, 403)
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('1.50'))

    def test_custody_return_view_updates(self):
        self._seed_custody()
        response = self.client.post(
            reverse('inventory:custody_return'),
            {
                'raw_material_id': self.raw.id,
                'held_by': self.worker1.id,
                'measured_quantity': '2.5',
                'note': 'پایان شیفت',
            },
            headers={'x-requested-with': 'XMLHttpRequest'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['quantity'], '2.50')
        self.assertEqual(payload['delta'], '1.00')
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('2.50'))

    def test_custody_return_view_accepts_json_body(self):
        """مرورگر JSON می‌فرستد؛ request.POST باید خالی باشد ولی کار کند."""
        self._seed_custody()
        response = self.client.post(
            reverse('inventory:custody_return'),
            data=self._json({
                'raw_material_id': self.raw.id,
                'held_by': self.worker1.id,
                'measured_quantity': '2.5',
                'note': 'پایان شیفت',
            }),
            content_type='application/json',
            headers={'x-requested-with': 'XMLHttpRequest'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('2.50'))

    def test_custody_return_view_rejects_unknown_material(self):
        self._seed_custody()
        response = self.client.post(
            reverse('inventory:custody_return'),
            {'raw_material_id': 999999, 'held_by': self.worker1.id, 'measured_quantity': '1'},
            headers={'x-requested-with': 'XMLHttpRequest'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response['Content-Type'].split(';')[0], 'application/json')

    def test_custody_return_view_rejects_negative(self):
        self._seed_custody()
        response = self.client.post(
            reverse('inventory:custody_return'),
            {
                'raw_material_id': self.raw.id,
                'held_by': self.worker1.id,
                'measured_quantity': '-3',
            },
            headers={'x-requested-with': 'XMLHttpRequest'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    def test_custody_board_view(self):
        self._seed_custody()
        response = self.client.get(reverse('inventory:custody_board'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_tab'], 'custody')
        self.assertContains(response, 'تحویل روزانه نقاشی')
        self.assertEqual(response.context['summary']['workers_count'], 1)

    def test_custody_board_filters(self):
        self._seed_custody()
        services.return_custody(
            raw_material=self.raw, held_by=self.worker1, measured_quantity='0',
            recorded_by=self.warehouse,
        )
        response = self.client.get(reverse('inventory:custody_board'), {'only_open': '1'})
        self.assertEqual(response.context['summary']['open_rows'], 0)
        self.assertEqual(len(response.context['rows']), 0)

        response = self.client.get(reverse('inventory:custody_board'))
        self.assertEqual(len(response.context['rows']), 1)

    def test_handover_preview_and_create_with_held_by(self):
        issue = self._issue(Decimal('3'))
        preview = self.client.post(
            reverse('inventory:handover_preview'),
            data=self._json({'items': [{'issue_id': issue.pk, 'quantity': '3'}], 'held_by': self.worker1.pk}),
            content_type='application/json',
        )
        rows = preview.json()['rows']
        self.assertTrue(rows[0]['custody_source'])
        self.assertEqual(rows[0]['new_custody'], '1.50')

        created = self.client.post(
            reverse('inventory:handover_create'),
            data=self._json({
                'items': [{'issue_id': issue.pk, 'quantity': '3'}],
                'received_by': self.worker1.pk,
                'held_by': self.worker1.pk,
            }),
            content_type='application/json',
        )
        self.assertTrue(created.json()['success'])
        self.assertTrue(
            MaterialCustody.objects.filter(raw_material=self.raw, held_by=self.worker1).exists()
        )
        self.assertFalse(MaterialLeftover.objects.filter(quantity__gt=0).exists())

    def test_handover_create_without_held_by_stays_legacy(self):
        issue = self._issue(Decimal('3'))
        created = self.client.post(
            reverse('inventory:handover_create'),
            data=self._json({
                'items': [{'issue_id': issue.pk, 'quantity': '3'}],
                'received_by': self.worker1.pk,
            }),
            content_type='application/json',
        )
        self.assertTrue(created.json()['success'])
        self.assertFalse(MaterialCustody.objects.exists())
        self.assertEqual(
            MaterialLeftover.objects.get(raw_material=self.raw).quantity, Decimal('1.50'),
        )

    @staticmethod
    def _json(payload):
        import json
        return json.dumps(payload)