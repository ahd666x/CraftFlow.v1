from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
import inspect

from product.models import Product, ProductCategory, Order, OrderItem, WorkerProfile
from product import utils


class Batch2B4LocalTimeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('admin', password='testpass')
        cls.category = ProductCategory.objects.create(name='دسته')

    def setUp(self):
        self.client.login(username='admin', password='testpass')

    def test_report_material_consumption_loads(self):
        response = self.client.get(reverse('report_material_consumption'))
        self.assertEqual(response.status_code, 200)

    def test_daily_schedule_print_uses_localtime(self):
        response = self.client.get(reverse('daily_schedule_print'))
        self.assertEqual(response.status_code, 200)

    def test_utils_uses_localtime_for_datestrings(self):
        source = inspect.getsource(utils)
        self.assertIn('timezone.localtime', source)

    def test_extract_paint_schedule_command_uses_localtime(self):
        import importlib
        cmd = importlib.import_module('product.management.commands.extract_paint_schedule')
        source = inspect.getsource(cmd)
        self.assertIn('timezone.localtime', source)

    def test_export_schedule_command_uses_localtime(self):
        import importlib
        cmd = importlib.import_module('product.management.commands.export_schedule')
        source = inspect.getsource(cmd)
        self.assertIn('timezone.localtime', source)
