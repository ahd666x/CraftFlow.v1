"""Package-F coverage: shared workbench edit templates (قالب ویرایش).

A template is a saved FORM, never an executable artifact: the API only
creates/updates/lists/deletes named payloads of interactive edits. Loading a
template prefills the workbench edit list and every edit still passes the
full fail-closed preview (runtime proof + collision gate) at USE time.
"""
import json

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from .models import EditTemplate


class EditTemplateApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("cnc-manager-tpl", password="testpass")
        manager_group = Group.objects.create(name="2")
        cls.manager.groups.add(manager_group)
        cls.regular = User.objects.create_user("cnc-user-tpl", password="testpass")

    def list_url(self):
        return reverse("cnc_resizer:edit_templates")

    def delete_url(self, template_id):
        return reverse("cnc_resizer:edit_template_delete", args=[template_id])

    def payload(self, **extra):
        data = {"name": "شبکهٔ ۳×۲ سوراخ",
                "edits": [{"op": "grid", "feature_id": "feature-block-0001",
                           "count_x": 2, "pitch_x": 30, "count_y": 1}],
                "note": "t.16"}
        data.update(extra)
        return data

    def test_requires_manager(self):
        response = self.client.get(self.list_url())
        self.assertEqual(response.status_code, 302)
        self.client.login(username="cnc-user-tpl", password="testpass")
        response = self.client.get(self.list_url())
        self.assertEqual(response.status_code, 403)
        response = self.client.post(self.list_url(), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_list_starts_empty(self):
        self.client.login(username="cnc-manager-tpl", password="testpass")
        response = self.client.get(self.list_url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["templates"], [])

    def test_create_and_round_trip(self):
        self.client.login(username="cnc-manager-tpl", password="testpass")
        response = self.client.post(self.list_url(), data=json.dumps(self.payload()),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["created"])
        self.assertEqual(EditTemplate.objects.get(pk=body["template_id"]).created_by,
                         self.manager)
        response = self.client.get(self.list_url())
        templates = response.json()["templates"]
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["name"], "شبکهٔ ۳×۲ سوراخ")
        self.assertEqual(templates[0]["payload"][0]["op"], "grid")
        self.assertEqual(templates[0]["payload"][0]["count_x"], 2)
        self.assertEqual(templates[0]["note"], "t.16")
        self.assertIn("updated_at", templates[0])

    def test_upsert_by_name_keeps_single_row(self):
        self.client.login(username="cnc-manager-tpl", password="testpass")
        first = self.client.post(self.list_url(), data=json.dumps(self.payload()),
                                 content_type="application/json")
        self.assertTrue(first.json()["created"])
        second = self.client.post(self.list_url(), data=json.dumps(self.payload(
            edits=[{"op": "shift", "feature_id": "feature-block-0002", "dx": 5, "dy": 10}],
            note="به‌روزشده")), content_type="application/json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertFalse(second.json()["created"])
        self.assertEqual(EditTemplate.objects.count(), 1)
        template = EditTemplate.objects.get(name="شبکهٔ ۳×۲ سوراخ")
        self.assertEqual(template.payload[0]["op"], "shift")
        self.assertEqual(template.note, "به‌روزشده")

    def test_validation_errors(self):
        self.client.login(username="cnc-manager-tpl", password="testpass")
        cases = [
            {"name": ""},
            {"name": "   "},
            {"edits": []},
            {"edits": "nope"},
            {"edits": None},
            {"edits": {"op": "shift"}},
            {"edits": [{"op": "rotate90", "feature_id": "feature-block-0001"}]},
            {"edits": [{"op": "shift"}]},                       # feature_id missing
            {"edits": [{"op": "shift", "feature_id": ""}]},     # feature_id empty
            {"edits": [{"op": "shift", "feature_id": 7}]},      # feature_id not a string
            {"edits": [["shift"]]},                             # edit not an object
            {"name": "x", "edits": [{"op": "shift", "feature_id": "f"}] * 65},  # too many
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                response = self.client.post(self.list_url(), data=json.dumps(self.payload(**extra)),
                                            content_type="application/json")
                self.assertEqual(response.status_code, 400, response.content)
                self.assertFalse(response.json()["ok"])
        self.assertEqual(EditTemplate.objects.count(), 0)

    def test_name_is_trimmed_and_capped(self):
        self.client.login(username="cnc-manager-tpl", password="testpass")
        response = self.client.post(self.list_url(), data=json.dumps(self.payload(name="  شبکه  ")),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(EditTemplate.objects.filter(name="شبکه").exists())
        response = self.client.post(self.list_url(), data=json.dumps(self.payload(name="ط" * 120)),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(EditTemplate.objects.filter(name="ط" * 80).exists())

    def test_delete_removes_and_404_on_repeat(self):
        self.client.login(username="cnc-manager-tpl", password="testpass")
        created = self.client.post(self.list_url(), data=json.dumps(self.payload()),
                                   content_type="application/json").json()
        template_id = created["template_id"]
        response = self.client.post(self.delete_url(template_id), data="{}",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("حذف شد", response.json()["message"])
        self.assertEqual(EditTemplate.objects.count(), 0)
        response = self.client.post(self.delete_url(template_id), data="{}",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_delete_requires_manager(self):
        self.client.login(username="cnc-user-tpl", password="testpass")
        response = self.client.post(self.delete_url(1), data="{}",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 403)
