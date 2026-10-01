"""تست‌های مدل «امانت مواد نزد کارگر» (MaterialCustody)."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from inventory import services
from inventory.models import (
    CustodyConsumption, MaterialCustody, MaterialCustodyReturn, MaterialHandover,
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
        result = services.return_custody(
            raw_material=self.raw, held_by=self.worker1, measured_quantity='2.5',
            recorded_by=self.warehouse, note='توزین پایان روز',
        )
        record = result['record']
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
        # حالت عادی پایان روز (توزین کمتر از امانت ثبت‌شده) نباید موجودی انبار
        # را جابه‌جا کند: کل بسته از لحظهٔ تحویل کسر شده و انتساب مصرف هم اثر
        # ندارد. حالت کسری و بازگشت به انبار جداگانه تست شده‌اند.
        self._issue_to(self.worker1)
        before = StockMovement.objects.count()
        services.return_custody(
            raw_material=self.raw, held_by=self.worker1, measured_quantity='0',
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

# ============================================================
# مجموع مواد نیاز تحویل + توزیع تناسبی
# ============================================================

class AggregateNeedsTests(CustodyTestBase):
    def _pending(self):
        # هر سه روی همان ماده، با مقدار متفاوت
        a = self._issue(Decimal('2'))
        b = self._issue(Decimal('6'))
        c = self._issue(Decimal('4'))
        return a, b, c

    def test_aggregate_needs_groups_by_raw_material(self):
        self._pending()
        qs = MaterialIssue.objects.filter(status__in=['requested', 'partial'])
        rows = services.aggregate_needs(qs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['raw_material_id'], self.raw.pk)
        self.assertEqual(rows[0]['need'], Decimal('12.00'))
        self.assertEqual(rows[0]['issue_count'], 3)

    def test_aggregate_needs_ignores_already_issued(self):
        issue = self._issue(Decimal('5'))
        issue.issued_quantity = Decimal('2')
        issue.status = 'partial'
        issue.save()
        qs = MaterialIssue.objects.filter(status__in=['requested', 'partial'])
        rows = services.aggregate_needs(qs)
        self.assertEqual(rows[0]['need'], Decimal('3.00'))

    def test_distribute_is_proportional_and_sums_exactly(self):
        a, b, c = self._pending()   # نیازها: 2، 6، 4  => مجموع 12
        items = services.distribute_aggregate(self.raw.pk, Decimal('6'))
        total = sum(q for _, q in items)
        self.assertEqual(total, Decimal('6.00'))
        mapping = dict(items)
        # نسبت‌ها 1:3:2 از ۶ یعنی ۱، ۳، ۲
        self.assertEqual(mapping[a.pk], Decimal('1.00'))
        self.assertEqual(mapping[b.pk], Decimal('3.00'))
        self.assertEqual(mapping[c.pk], Decimal('2.00'))

    def test_distribute_rounding_never_loses_a_cent(self):
        # نیازهایی که نسبتشان بخش‌پذیر نیست: 1 / 1 / 1
        for _ in range(3):
            self._issue(Decimal('1'))
        items = services.distribute_aggregate(self.raw.pk, Decimal('1'))
        self.assertEqual(sum(q for _, q in items), Decimal('1.00'))

    def test_distribute_full_need_returns_each_issue_whole(self):
        a, b, c = self._pending()
        items = services.distribute_aggregate(self.raw.pk, Decimal('12'))
        self.assertEqual(len(items), 3)
        self.assertEqual(sum(q for _, q in items), Decimal('12.00'))

    def test_distribute_rejects_more_than_total_need(self):
        self._pending()
        with self.assertRaises(HandoverError):
            services.distribute_aggregate(self.raw.pk, Decimal('12.01'))

    def test_distribute_skips_issued_issues(self):
        a, b, _ = self._pending()
        a.issued_quantity = Decimal('2')
        a.status = 'issued'
        a.save()
        items = services.distribute_aggregate(self.raw.pk, Decimal('10'))
        self.assertNotIn(a.pk, [i for i, _ in items])

    def test_distribute_rejects_non_positive_quantity(self):
        self._pending()
        with self.assertRaises(HandoverError):
            services.distribute_aggregate(self.raw.pk, Decimal('0'))


# ============================================================
# انتساب مصرف پایان روز به خرابی‌ها
# ============================================================

class CustodyConsumptionTests(CustodyTestBase):
    def setUp(self):
        super().setUp()
        self.defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, color_part='بدنه',
            quantity=1, description='خط و خش', reported_by=self.warehouse,
        )
        self.other_defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, color_part='درب',
            quantity=1, description='پوست شدن', reported_by=self.warehouse,
        )
        MaterialCustody.objects.create(
            raw_material=self.raw, held_by=self.worker1, quantity=Decimal('4'),
        )

    def test_attribution_is_stored_without_touching_stock(self):
        stock_before = self.raw.current_stock
        result = services.return_custody(
            raw_material=self.raw, held_by=self.worker1,
            measured_quantity=Decimal('2.5'), recorded_by=self.warehouse,
            attributions=[(self.defect, Decimal('1.0')),
                          (self.other_defect, Decimal('0.5'))],
        )
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.current_stock, stock_before)
        self.assertEqual(result['consumed'], Decimal('1.50'))
        self.assertEqual(result['attributed_total'], Decimal('1.50'))
        self.assertEqual(CustodyConsumption.objects.count(), 2)
        stored = CustodyConsumption.objects.get(defect=self.defect)
        self.assertEqual(stored.quantity, Decimal('1.00'))
        self.assertEqual(stored.held_by, self.worker1)
        self.assertEqual(stored.raw_material, self.raw)

    def test_consumed_is_independent_from_warehouse_return(self):
        stock_before = self.raw.current_stock
        # امانت قبلی ۴، توزین ۲، از این ۲ مقدار ۱.۵ به انبار برگشت.
        # مصرف = ۴ − ۲ = ۲ (نه ۴ − ۱.۵)؛ باقی‌مانده نزد کارگر = ۲.
        result = services.return_custody(
            raw_material=self.raw, held_by=self.worker1,
            measured_quantity=Decimal('2'), recorded_by=self.warehouse,
            to_warehouse=Decimal('1.5'),
            attributions=[(self.defect, Decimal('2'))],
        )
        self.raw.refresh_from_db()
        self.assertEqual(result['consumed'], Decimal('2.00'))
        self.assertEqual(result['warehouse_return'], Decimal('1.50'))
        self.assertEqual(self.raw.current_stock, stock_before + Decimal('1.50'))
        movement = StockMovement.objects.filter(movement_type='return').latest('id')
        self.assertEqual(movement.quantity, Decimal('1.50'))
        custody = MaterialCustody.objects.get(raw_material=self.raw, held_by=self.worker1)
        self.assertEqual(custody.quantity, Decimal('2.00'))

    def test_warehouse_return_cannot_exceed_measured(self):
        with self.assertRaises(HandoverError):
            services.return_custody(
                raw_material=self.raw, held_by=self.worker1,
                measured_quantity=Decimal('1'), recorded_by=self.warehouse,
                to_warehouse=Decimal('1.5'),
            )

    def test_partial_attribution_is_allowed(self):
        result = services.return_custody(
            raw_material=self.raw, held_by=self.worker1,
            measured_quantity=Decimal('1'), recorded_by=self.warehouse,
            attributions=[(self.defect, Decimal('0.5'))],
        )
        self.assertEqual(result['consumed'], Decimal('3.00'))
        self.assertEqual(result['attributed_total'], Decimal('0.50'))
        self.assertEqual(result['shortfall'], Decimal('2.50'))

    def test_over_attribution_is_rejected(self):
        with self.assertRaises(HandoverError):
            services.return_custody(
                raw_material=self.raw, held_by=self.worker1,
                measured_quantity=Decimal('1'), recorded_by=self.warehouse,
                attributions=[(self.defect, Decimal('9'))],
            )

    def test_duplicate_defect_in_same_return_is_rejected(self):
        with self.assertRaises(HandoverError):
            services.return_custody(
                raw_material=self.raw, held_by=self.worker1,
                measured_quantity=Decimal('1'), recorded_by=self.warehouse,
                attributions=[(self.defect, Decimal('1')),
                              (self.defect, Decimal('1'))],
            )

    def test_more_material_than_recorded_credits_stock_back(self):
        # امانت ثبت‌شده ۴ ولی توزین ۶ ⇒ ۲ کیلو رنگ هیچ‌وقت از انبار کسر نشده
        # بود، پس موجودی انبار ۲ واحد به آن برمی‌گردد.
        stock_before = self.raw.current_stock
        result = services.return_custody(
            raw_material=self.raw, held_by=self.worker1,
            measured_quantity=Decimal('6'), recorded_by=self.warehouse,
        )
        self.assertEqual(result['consumed'], Decimal('-2.00'))
        movement = StockMovement.objects.filter(movement_type='adjustment').latest('id')
        self.assertEqual(movement.quantity, Decimal('2.00'))
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.current_stock, stock_before + Decimal('2.00'))

    def test_warehouse_return_wins_over_credit(self):
        # هم بازگشت به انبار و هم کسری: فقط حرکت return ساخته می‌شود.
        stock_before = self.raw.current_stock
        services.return_custody(
            raw_material=self.raw, held_by=self.worker1,
            measured_quantity=Decimal('6'), recorded_by=self.warehouse,
            to_warehouse=Decimal('1'),
        )
        self.assertFalse(StockMovement.objects.filter(movement_type='adjustment').exists())
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.current_stock, stock_before + Decimal('1.00'))

    def test_return_custody_does_not_double_deduct_consumption(self):
        stock_before = self.raw.current_stock
        services.return_custody(
            raw_material=self.raw, held_by=self.worker1,
            measured_quantity=Decimal('0'), recorded_by=self.warehouse,
            attributions=[(self.defect, Decimal('4'))],
        )
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.current_stock, stock_before)


# ============================================================
# ویوی ثبت بازگشت با انتساب
# ============================================================

class CustodyReturnAttributionViewTests(CustodyTestBase):
    def setUp(self):
        super().setUp()
        self.defect = ProductionDefect.objects.create(
            order=self.order, order_item=self.order_item, color_part='بدنه',
            quantity=1, description='خط و خش', reported_by=self.warehouse,
        )
        MaterialCustody.objects.create(
            raw_material=self.raw, held_by=self.worker1, quantity=Decimal('3'),
        )

    def _post(self, payload):
        import json
        return self.client.post(
            reverse('inventory:custody_return'),
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_view_accepts_attributions_and_warehouse_return(self):
        # امانت قبلی ۳، توزین ۱.۵ ⇒ مصرف ۱.۵ (سقف انتساب)
        response = self._post({
            'raw_material_id': self.raw.pk,
            'held_by': self.worker1.pk,
            'measured_quantity': '1.5',
            'to_warehouse': '0.5',
            'attributions': [{'defect_id': self.defect.pk, 'quantity': '1.5'}],
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['consumed'], '1.50')
        self.assertEqual(body['warehouse_return'], '0.50')
        self.assertEqual(body['attributed'], '1.50')
        self.assertEqual(CustodyConsumption.objects.count(), 1)

    def test_view_rejects_over_attribution(self):
        response = self._post({
            'raw_material_id': self.raw.pk,
            'held_by': self.worker1.pk,
            'measured_quantity': '2',
            'attributions': [{'defect_id': self.defect.pk, 'quantity': '99'}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.assertFalse(CustodyConsumption.objects.exists())

    def test_view_rejects_unknown_defect(self):
        response = self._post({
            'raw_material_id': self.raw.pk,
            'held_by': self.worker1.pk,
            'measured_quantity': '1',
            'attributions': [{'defect_id': 987654, 'quantity': '1'}],
        })
        self.assertEqual(response.status_code, 400)

    def test_view_works_without_any_attribution(self):
        response = self._post({
            'raw_material_id': self.raw.pk,
            'held_by': self.worker1.pk,
            'measured_quantity': '1',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(CustodyConsumption.objects.count(), 0)

    def test_defect_choices_endpoint(self):
        import json
        response = self.client.post(
            reverse('inventory:defect_choices'),
            data=json.dumps({'raw_material_id': self.raw.pk}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        ids = [o['id'] for o in response.json()['options']]
        self.assertNotIn(self.defect.pk, ids)  # این خرابی هنوز درخواست مواد ندارد

        MaterialIssue.objects.create(
            defect=self.defect, raw_material=self.raw, requested_quantity=Decimal('1'),
            purpose='rework', status='requested', requested_by=self.warehouse,
        )
        response = self.client.post(
            reverse('inventory:defect_choices'),
            data=json.dumps({'raw_material_id': self.raw.pk}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        ids = [o['id'] for o in response.json()['options']]
        self.assertIn(self.defect.pk, ids)

    def test_defect_choices_requires_valid_material(self):
        import json
        response = self.client.post(
            reverse('inventory:defect_choices'),
            data=json.dumps({'raw_material_id': 'abc'}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 400)


# ============================================================
# ویوی تحویل مجموعی
# ============================================================

class AggregateHandoverViewTests(CustodyTestBase):
    def _post(self, name, payload):
        import json
        return self.client.post(
            reverse(f'inventory:{name}'),
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_queue_page_shows_aggregate_block(self):
        for _ in range(3):
            self._issue(Decimal('2'))
        response = self.client.get(reverse('inventory:production_issue_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aggregateTable')
        self.assertContains(response, 'مجموع مواد نیاز تحویل')
        self.assertEqual(len(response.context['aggregate_rows']), 1)
        self.assertEqual(response.context['aggregate_rows'][0]['need'], Decimal('6.00'))

    def test_aggregate_preview_reports_distribution(self):
        a, b, c = self._issue(Decimal('2')), self._issue(Decimal('6')), self._issue(Decimal('4'))
        response = self._post('aggregate_handover_preview', {
            'raw_material_id': self.raw.pk,
            'quantity': '6',
            'received_by': self.worker1.pk,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        allocation = {row['issue_id']: row['quantity'] for row in body['allocation']}
        self.assertEqual(allocation[a.pk], '1.00')
        self.assertEqual(allocation[b.pk], '3.00')
        self.assertEqual(allocation[c.pk], '2.00')
        self.assertTrue(body['rows'])

    def test_aggregate_preview_requires_receiver(self):
        self._issue(Decimal('2'))
        response = self._post('aggregate_handover_preview', {
            'raw_material_id': self.raw.pk, 'quantity': '1',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])

    def test_aggregate_create_splits_across_issues(self):
        a, b, c = self._issue(Decimal('2')), self._issue(Decimal('6')), self._issue(Decimal('4'))
        response = self._post('aggregate_handover_create', {
            'raw_material_id': self.raw.pk,
            'quantity': '6',
            'received_by': self.worker1.pk,
            'held_by': self.worker1.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['rows'], 3)
        a.refresh_from_db(); b.refresh_from_db(); c.refresh_from_db()
        self.assertEqual(a.issued_quantity, Decimal('1'))
        self.assertEqual(b.issued_quantity, Decimal('3'))
        self.assertEqual(c.issued_quantity, Decimal('2'))
        self.assertEqual(a.status, 'partial')
        self.assertEqual(b.status, 'partial')

    def test_aggregate_create_rejects_overshoot(self):
        self._issue(Decimal('2'))
        response = self._post('aggregate_handover_create', {
            'raw_material_id': self.raw.pk,
            'quantity': '5',
            'received_by': self.worker1.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertFalse(MaterialHandover.objects.exists())
