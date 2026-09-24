import os
from django.test import TestCase, override_settings
from django.conf import settings
from django.contrib.auth.models import User


class Batch2A5SettingsTests(TestCase):
    def test_allowed_hosts_no_wildcard(self):
        self.assertNotIn('*', settings.ALLOWED_HOSTS)

    def test_media_protected_anonymous_redirects(self):
        response = self.client.get('/media/test.png')
        self.assertIn(response.status_code, (302, 301))

    def test_media_protected_authenticated_404(self):
        user = User.objects.create_user('user_a5', password='testpass')
        self.client.login(username='user_a5', password='testpass')
        response = self.client.get('/media/nonexistent_file_99a8b7c6.png')
        self.assertEqual(response.status_code, 404)

    def test_storages_configured_correctly(self):
        self.assertIn('staticfiles', settings.STORAGES)
        self.assertEqual(
            settings.STORAGES['staticfiles']['BACKEND'],
            'whitenoise.storage.CompressedStaticFilesStorage'
        )
        self.assertIn('default', settings.STORAGES)
        self.assertEqual(
            settings.STORAGES['default']['BACKEND'],
            'django.core.files.storage.FileSystemStorage'
        )

    def test_media_root_uses_env_or_default(self):
        # Either from env or the gateway default — must exist as a string path
        self.assertIsInstance(settings.MEDIA_ROOT, str)
        self.assertTrue(len(settings.MEDIA_ROOT) > 0)
