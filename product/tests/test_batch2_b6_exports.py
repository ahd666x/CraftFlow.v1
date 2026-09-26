import xml.etree.ElementTree as ET

from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
    Customer, Material, Part, ProductBOM, ProductionEvent, ProductionLog,
)


class Batch2B6ExportsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')
        cls.material = Material.objects.create(name='MDF-18', thickness='18.00')
        cls.part = Part.objects.create(
            material=cls.material, name='قطعه تست', length='100.0',
            width='50.0', grain='ردیف اول', pname='محصول تست',
            turn=False, f2='F2', routing_code='cut',
        )
        cls.product = Product.objects.create(
            category=cls.category, name='محصول', base_price=1000,
            default_size='50', price_increment_per_cm=0,
        )
        ProductBOM.objects.create(product=cls.product, part=cls.part, quantity=1)
        cls.customer = Customer.objects.create(name='مشتری', phone='09120000000')

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def _create_order_with_cut_task(self, number):
        order = Order.objects.create(user=self.admin, customer=self.customer, number=number)
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        ProductionTask.objects.create(
            order=order, order_item=item, part=self.part,
            station_name='cut', step_order=1, quantity=1, status='pending',
        )
        return order, item

    def test_export_autocut_xml_contains_grain_and_order_from_part(self):
        order, _ = self._create_order_with_cut_task('B6-1')
        response = self.client.get(reverse('export_autocut_xml', args=[order.id]))
        self.assertEqual(response.status_code, 200)
        root = ET.fromstring(response.content)
        ns = {'ns': 'http://www.King-stone.com'}
        shapes = root.findall('.//ns:Shape', ns)
        self.assertTrue(len(shapes) > 0)
        shape = shapes[0]
        self.assertEqual(shape.get('Grain'), 'ردیف اول')
        self.assertEqual(shape.get('Order'), 'محصول تست')

    def test_export_multiple_autocut_works(self):
        order, _ = self._create_order_with_cut_task('B6-2')
        response = self.client.post(reverse('export_multiple_autocut'), {'order_ids': [order.id]})
        self.assertEqual(response.status_code, 200)
        root = ET.fromstring(response.content)
        ns = {'ns': 'http://www.King-stone.com'}
        shapes = root.findall('.//ns:Shape', ns)
        self.assertTrue(len(shapes) > 0)
        shape = shapes[0]
        self.assertEqual(shape.get('Grain'), 'ردیف اول')
        self.assertEqual(shape.get('Order'), 'محصول تست')

    def test_report_production_unified_uses_jalali_dates(self):
        order = Order.objects.create(user=self.admin, customer=self.customer, number='B6-3')
        item = OrderItem.objects.create(order=order, product=self.product, quantity=1)
        task = ProductionTask.objects.create(
            order=order, order_item=item,
            station_name='paint', step_order=1, quantity=1, status='pending',
        )
        ProductionEvent.objects.create(
            task=task, order=order, order_item=item,
            station_name='paint', event_type='done', user=self.admin,
            old_status='pending', new_status='done',
        )
        response = self.client.get(reverse('report_production_unified'))
        self.assertEqual(response.status_code, 200)
