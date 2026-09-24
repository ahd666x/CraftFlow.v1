import json

from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from decimal import Decimal

from product.utils import invalidate_caches
from product.models import (
    PaintingProcess, PaintingProcessMaterial, PaintingColorMaterialVariant,
)
from inventory.models import RawMaterial, RawMaterialCategory


class ColorVariantAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('cvuser', password='testpass')
        cls.cat = RawMaterialCategory.objects.create(name='رنگ و مواد نقاشی')
        cls.raw = RawMaterial.objects.create(
            category=cls.cat, name='رنگ کد ۸', code='CV01', unit='lit',
        )
        cls.process = PaintingProcess.objects.create(
            name='روند کد', code='CV', color_codes=['8'], is_active=True,
        )
        cls.pm = PaintingProcessMaterial.objects.create(
            process=cls.process, raw_material=cls.raw, is_color_variant=True,
        )

    def setUp(self):
        invalidate_caches()
        self.client.login(username='cvuser', password='testpass')

    def test_post_valid_color_code(self):
        url = reverse('painting_process_material_variants_api', args=[self.pm.id])
        response = self.client.post(
            url,
            data=json.dumps({'color_code': '8', 'raw_material_id': self.raw.id}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['color_code'], '8')
        self.assertEqual(data['color_label'], '8')

    def test_post_invalid_color_code(self):
        url = reverse('painting_process_material_variants_api', args=[self.pm.id])
        response = self.client.post(
            url,
            data=json.dumps({'color_code': '99', 'raw_material_id': self.raw.id}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('کد رنگ', data['error'])