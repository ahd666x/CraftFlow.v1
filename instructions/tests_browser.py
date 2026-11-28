"""تست‌های مرورگر بندها: گروه‌بندی بر اساس محصول و نمایش محصول‌های بدون بند."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from instructions.models import WorkInstruction
from instructions.tests import _base_data
from product.models import Part, Product


class KnowledgeBrowserGroupingTests(TestCase):
    """مرورگر: هر محصول با بندهای خودش، محصول بدون بند هم دیده می‌شود."""

    @classmethod
    def setUpTestData(cls):
        _base_data(cls)
        cls.staff = User.objects.create_user('staffer4', password='x', is_staff=True)
        cls.other = Product.objects.create(category=cls.category, name='کنسول آرمان', base_price=1000)
        cls.empty = Product.objects.create(category=cls.category, name='محصول تازه', base_price=1000)
        WorkInstruction.objects.create(product=cls.product, station='cnc', title='بند فیدان')
        WorkInstruction.objects.create(product=cls.other, station='pvc', kind=WorkInstruction.KIND_WARNING,
                                       title='بند آرمان')
        part = Part.objects.create(material=cls.mdf_raw, name='قطعه جدا', pname='x',
                                   length=10, width=10, routing_code='cnc')
        WorkInstruction.objects.create(part=part, station='dr', title='بند قطعه')

    def _groups(self, params=None):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('instructions:browser'), params or {})
        self.assertEqual(resp.status_code, 200)
        return resp, {g['product'].name: g for g in resp.context['groups']}

    def test_lists_every_product_including_empty(self):
        resp, groups = self._groups()
        self.assertEqual(set(groups), {'میز فیدان', 'کنسول آرمان', 'محصول تازه'})
        self.assertContains(resp, 'شروع نوشتن')
        self.assertEqual(resp.context['stats']['empty_products'], 1)

    def test_instructions_stay_under_their_own_product(self):
        _resp, groups = self._groups()
        titles = {name: [i.title for st in g['stations'] for i in st['items']] for name, g in groups.items()}
        self.assertEqual(titles['میز فیدان'], ['بند فیدان'])
        self.assertEqual(titles['کنسول آرمان'], ['بند آرمان'])
        self.assertEqual(titles['محصول تازه'], [])

    def test_collapsed_by_default_and_open_when_filtered(self):
        _resp, groups = self._groups()
        self.assertFalse(any(g['open'] for g in groups.values()))
        _resp, groups = self._groups({'kind': 'warning'})
        self.assertEqual(set(groups), {'کنسول آرمان'})
        self.assertTrue(groups['کنسول آرمان']['open'])

    def test_product_filter_shows_only_that_product(self):
        resp, groups = self._groups({'product': self.product.pk})
        self.assertEqual(set(groups), {'میز فیدان'})
        self.assertEqual(resp.context['part_groups'], [])

    def test_empty_status_lists_only_products_without_instructions(self):
        resp, groups = self._groups({'status': 'empty'})
        self.assertEqual(set(groups), {'محصول تازه'})
        self.assertEqual(resp.context['rows'], [])

    def test_part_level_instructions_in_own_section(self):
        resp, _groups = self._groups()
        self.assertEqual([g['part'].name for g in resp.context['part_groups']], ['قطعه جدا'])
        self.assertContains(resp, 'بند قطعه')
        _resp, groups = self._groups()
        self.assertNotIn('بند قطعه', [i.title for g in groups.values() for st in g['stations'] for i in st['items']])
