from django.test import TestCase
from django.contrib.auth.models import User

from product.forms import PaintingProcessForm, WorkerProfileForm
from product.models import PaintingProcess


class PaintingProcessFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('formuser', password='testpass')
        cls.existing = PaintingProcess.objects.create(
            name='روند موجود', code='EXIST', color_codes=['8', '9'], is_active=True,
        )

    def test_valid_json_string(self):
        form = PaintingProcessForm(data={
            'name': 'روند جدید', 'code': 'NEW', 'color_codes': '["10","11"]',
            'is_active': True, 'description': '',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['color_codes'], ['10', '11'])

    def test_empty_color_codes(self):
        form = PaintingProcessForm(data={
            'name': 'روند خالی', 'code': 'EMPTY', 'color_codes': '',
            'is_active': True, 'description': '',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['color_codes'], [])

    def test_broken_json_raises_error(self):
        form = PaintingProcessForm(data={
            'name': 'روند خراب', 'code': 'BROKEN', 'color_codes': '{not json',
            'is_active': True, 'description': '',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('color_codes', form.errors)

    def test_overlap_with_active_process(self):
        form = PaintingProcessForm(data={
            'name': 'روند هم‌پوشان', 'code': 'OVERLAP', 'color_codes': '["8"]',
            'is_active': True, 'description': '',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('color_codes', form.errors)


class WorkerProfileFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser('wpuser', password='testpass')

    def test_valid_skills_and_priority(self):
        form = WorkerProfileForm(data={
            'user': self.user.id, 'stage': 'paint',
            'skills': '["painter"]', 'skill_priority': '{"painter":3}',
            'is_available': True, 'excluded_products': [],
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['skills'], ['painter'])
        self.assertEqual(form.cleaned_data['skill_priority'], {'painter': 3})