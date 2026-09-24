from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from decimal import Decimal

from inventory.models import RawMaterial, RawMaterialCategory, StockMovement
from inventory.forms import StockMovementForm


class Batch2A4ReceiveValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_superuser('manager', password='testpass')
        cls.cat = RawMaterialCategory.objects.create(name='مواد')
        cls.raw = RawMaterial.objects.create(
            category=cls.cat, name='ماده', code='RM1', unit='kg',
            pack_size=Decimal('4'), barcode='BRC001',
        )

    def setUp(self):
        self.client.login(username='manager', password='testpass')

    # --- View validation ---
    def test_pack_count_negative_view(self):
        before = StockMovement.objects.count()
        response = self.client.post(
            reverse('inventory:raw_material_receive_scan'),
            {'barcode': 'BRC001', 'pack_count': '-3'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_pack_count_zero_view(self):
        before = StockMovement.objects.count()
        response = self.client.post(
            reverse('inventory:raw_material_receive_scan'),
            {'barcode': 'BRC001', 'pack_count': '0'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_pack_count_too_large_view(self):
        before = StockMovement.objects.count()
        response = self.client.post(
            reverse('inventory:raw_material_receive_scan'),
            {'barcode': 'BRC001', 'pack_count': '5000'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_pack_count_valid_creates_movement(self):
        before = StockMovement.objects.count()
        response = self.client.post(
            reverse('inventory:raw_material_receive_scan'),
            {'barcode': 'BRC001', 'pack_count': '2'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockMovement.objects.count(), before + 1)
        movement = StockMovement.objects.latest('id')
        self.assertEqual(movement.quantity, Decimal('8'))

    # --- API validation ---
    def _api_post(self, pack_count):
        return self.client.post(
            reverse('inventory:raw_material_receive_scan_api'),
            {'barcode': 'BRC001', 'pack_count': pack_count},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_api_pack_count_negative(self):
        before = StockMovement.objects.count()
        response = self._api_post('-3')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_api_pack_count_zero(self):
        before = StockMovement.objects.count()
        response = self._api_post('0')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_api_pack_count_too_large(self):
        before = StockMovement.objects.count()
        response = self._api_post('5000')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_api_pack_count_invalid_string(self):
        before = StockMovement.objects.count()
        response = self._api_post('abc')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StockMovement.objects.count(), before)

    def test_api_pack_count_valid(self):
        before = StockMovement.objects.count()
        response = self._api_post('2')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(StockMovement.objects.count(), before + 1)
        movement = StockMovement.objects.latest('id')
        self.assertEqual(movement.quantity, Decimal('8'))

    # --- StockMovementForm.clean guard ---
    def test_form_consumption_empty_quantity_no_crash(self):
        form = StockMovementForm(data={
            'raw_material': self.raw.id,
            'movement_type': 'consumption',
            'quantity': '',
        })
        self.assertFalse(form.is_valid())
