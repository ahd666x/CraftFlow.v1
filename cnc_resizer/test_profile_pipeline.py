# ============================================================
# تست‌های خط لولهٔ «الگوی تأییدشدهٔ تغییر اندازهٔ خودکار قطعه»
# (CncPartProfile) — پرامپت جامع اپراتور:
#   بخش ۳/۴: اتصال API پروفایل به ادیتور (URL در __CNC_URLS__ + ثبت/به‌روزرسانی)
#   بخش ۵:  اعتبارسنجی هندسه قبل از تأیید (پروب موتور روی فایل پایه)
#   بخش ۶:  استفادهٔ خودکار تسک بعدی از الگو (دلتای مخصوص هر تسک، بدون
#           دوباره‌دلتا، بدون تغییر فایل منبع، fallback صف دستی)
#   بخش ۹:  پوشش API / اتصال / جریان خودکار / هندسه
# ============================================================
import json
import os
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from cutting.models import SheetStock
from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CncFile, CncPartProfile
from .services import (
    CncResolutionError,
    register_base_file,
    resolve_cnc_file,
)
from .sheet_nesting import _manual_reason_code, plan_cnc_with_report
from .tests import SAMPLE


def _sample_text():
    with open(SAMPLE, encoding="utf-8-sig") as stream:
        return stream.read()


class ProfileApiFixtureMixin:
    """Manager user + base/dynamic CNC parts + an indexed resize-capable file.

    sample.cnc has 5 detectable gaps (see ResizeTests manual[0,0,-80,0,0]);
    base length 1740, dynamic length 1890 (delta +150).
    """

    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("profile-manager", password="testpass")
        cls.manager.groups.add(Group.objects.get_or_create(name="2")[0])
        cls.regular = User.objects.create_user("profile-user", password="testpass")

    def setUp(self):
        self.material = Material.objects.create(name="MDF پروفایل", thickness=Decimal("16.0"))
        self.base = Part.objects.create(
            material=self.material, name="پایه", length=Decimal("1740.0"),
            width=Decimal("400.0"), f3="panel.profile.1740x400", routing_code="cnc",
        )
        self.dynamic = Part.objects.create(
            material=self.material, name="بلند", length=Decimal("1890.0"),
            width=Decimal("400.0"), f3="panel.profile.1890x400", routing_code="cnc",
            base_part=self.base,
        )
        self.archive = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.archive, ignore_errors=True)
        base_path = os.path.join(self.archive, "panel.profile.1740x400.cnc")
        with open(base_path, "w", encoding="utf-8") as stream:
            stream.write(_sample_text().replace("N10G90G54", "N10G21G90G54", 1))
        self.source = register_base_file(base_path, self.base.f3, self.manager)
        self.assertTrue(self.source.resizable)

    def _login_manager(self):
        self.client.force_login(self.manager)

    def _profile_url(self, part=None):
        return reverse("cnc_resizer:part_profile") + f"?part_id={(part or self.dynamic).pk}"

    def _get_profile(self, part=None):
        response = self.client.get(self._profile_url(part))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _save_profile(self, params, part=None, expect=200):
        data = self._get_profile(part)
        response = self.client.post(
            self._profile_url(part),
            data=json.dumps({"part_id": (part or self.dynamic).pk,
                             "source_id": data["source_id"], "params": params}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, expect, response.content)
        return response, data


class ProfileApiTests(ProfileApiFixtureMixin, TestCase):
    """بخش ۹ پرامپت — API پروفایل: ساخت/به‌روزرسانی/بدون تکرار/رد نامعتبر/دسترسی."""

    def test_get_without_profile_returns_safe_defaults(self):
        self._login_manager()
        data = self._get_profile()
        self.assertFalse(data["approved"])
        self.assertEqual(data["params"], {"mode": "equal", "symmetric": True, "axis": "length"})
        self.assertFalse(data["profile_inherited"])
        self.assertEqual(data["source_id"], self.source.pk)
        self.assertIn("G", data["content"])

    def test_get_inherits_approved_profile_from_base_part(self):
        CncPartProfile.objects.create(
            part=self.base, source_file=self.source,
            params={"mode": "proportional", "symmetric": False, "axis": "length"},
            approved=True, approved_by=self.manager,
        )
        self._login_manager()
        data = self._get_profile()
        self.assertTrue(data["approved"])
        self.assertTrue(data["profile_inherited"])
        self.assertEqual(data["params"]["mode"], "proportional")

    def test_rejects_task_specific_delta_as_permanent_rule(self):
        """delta مخصوص هر تسک است (resolve از طول مؤثر می‌سازد) — ثبت آن
        به‌عنوان قاعدهٔ مهندسی سخت رد می‌شود، بی‌صدا strip نمی‌شود."""
        self._login_manager()
        response, _data = self._save_profile(
            {"mode": "equal", "symmetric": True, "delta": 150}, expect=400)
        self.assertIn("delta", response.json()["error"])
        self.assertFalse(CncPartProfile.objects.exists())

    def test_save_with_enabled_list_is_approved_and_clean(self):
        self._login_manager()
        response, _data = self._save_profile({
            "mode": "equal", "symmetric": True, "axis": "length",
            "enabled": [True] * 5,
        })
        self.assertTrue(response.json()["success"])
        profile = CncPartProfile.objects.get(part=self.dynamic)
        self.assertTrue(profile.approved)
        self.assertEqual(profile.approved_by, self.manager)
        self.assertNotIn("delta", profile.params)   # قاعدهٔ مهندسی، نه دلتای تسک
        self.assertEqual(profile.params["mode"], "equal")
        self.assertEqual(profile.params["enabled"], [True] * 5)
        self.assertEqual(profile.source_file_id, self.source.pk)

    def test_save_mirrors_to_base_part_and_update_never_duplicates(self):
        self._login_manager()
        self._save_profile({"mode": "equal", "symmetric": True})
        self._save_profile({"mode": "proportional", "symmetric": False})
        self.assertEqual(CncPartProfile.objects.count(), 2)   # dynamic + base، بیشتر نه
        for part in (self.dynamic, self.base):
            profile = CncPartProfile.objects.get(part=part)
            self.assertTrue(profile.approved)
            self.assertEqual(profile.params["mode"], "proportional")

    def test_rejects_invalid_mode_without_creating_profile(self):
        self._login_manager()
        response, _data = self._save_profile({"mode": "/stretch/", "symmetric": True}, expect=400)
        self.assertIn("روش توزیع نامعتبر", response.json()["error"])
        self.assertFalse(CncPartProfile.objects.exists())

    def test_rejects_unknown_param_keys(self):
        self._login_manager()
        # کلید 'profile' core.process را به مسیر ویژگی‌محور می‌برد — ممنوع
        response, _data = self._save_profile({"mode": "equal", "profile": {"axis": "y"}}, expect=400)
        self.assertIn("کلیدهای ناشناخته", response.json()["error"])
        self.assertFalse(CncPartProfile.objects.exists())

    def test_rejects_enabled_arity_mismatch_with_file_gaps(self):
        self._login_manager()
        response, _data = self._save_profile(
            {"mode": "equal", "symmetric": True, "enabled": [True]}, expect=400)
        self.assertIn("تعداد enabled", response.json()["error"])
        self.assertFalse(CncPartProfile.objects.exists())

    def test_rejects_manual_mode_without_manual_list(self):
        self._login_manager()
        response, _data = self._save_profile({"mode": "manual", "symmetric": True}, expect=400)
        self.assertIn("manual", response.json()["error"])

    def test_rejects_manual_array_that_breaks_gap_geometry(self):
        self._login_manager()
        # پروب واقعی موتور: فاصلهٔ سوم با −۱۰۰۰۰ منفی می‌شود → fail-closed
        response, _data = self._save_profile(
            {"mode": "manual", "manual": [0, 0, -10000, 0, 0]}, expect=400)
        self.assertIn("تأیید نمی‌کند", response.json()["error"])
        self.assertFalse(CncPartProfile.objects.exists())

    def test_rejects_bad_tool_dia_and_non_boolean_symmetric(self):
        self._login_manager()
        response, _data = self._save_profile({"mode": "equal", "tool_dia": 0}, expect=400)
        self.assertIn("قطر ابزار", response.json()["error"])
        response, _data = self._save_profile({"mode": "equal", "symmetric": "yes"}, expect=400)
        self.assertIn("تقارن", response.json()["error"])

    def test_part_not_found_is_404(self):
        self._login_manager()
        response = self.client.get(reverse("cnc_resizer:part_profile") + "?part_id=999999")
        self.assertEqual(response.status_code, 404)

    def test_requires_cnc_operator_group(self):
        response = self.client.get(self._profile_url())
        self.assertEqual(response.status_code, 302)   # anonymous → login
        self.client.force_login(self.regular)
        response = self.client.get(self._profile_url())
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            self._profile_url(), data=json.dumps({}),
            content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CncPartProfile.objects.exists())


class ProfileFrontendWiringTests(ProfileApiFixtureMixin, TestCase):
    """بخش ۳ پرامپت — URL پروفایل از __CNC_URLS__ به فرانت می‌رسد و ثبتِ
    رد‌شده هیچ وضعیت موفقیت ساختگی‌ای تولید نمی‌کند."""

    def test_index_exposes_profile_url_in_cnc_urls(self):
        self._login_manager()
        page = self.client.get(reverse("cnc_resizer:index"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'profile: "/cnc/api/part-profile/"')

    def test_failed_validation_leaves_no_approved_profile_behind(self):
        self._login_manager()
        self._save_profile({"mode": "equal", "enabled": [True, True]}, expect=400)
        self.assertEqual(CncPartProfile.objects.count(), 0)
        data = self._get_profile()
        self.assertFalse(data["approved"])   # UI «بدون الگو» را صادقانه نشان می‌دهد


class ManualQueueFlowMixin(ProfileApiFixtureMixin):
    """فیکسچر جریان خودکار: همان پایه/مشتقِ ProfileApiFixtureMixin + سفارش،
    تسک تولید و ورق — برای تستِ «الگوی ذخیره‌شده از مسیر API → برنامه‌ریز»."""

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(name="مشتری پروفایل")
        self.order = Order.objects.create(customer=self.customer, status="planned")
        self.task = ProductionTask.objects.create(
            order=self.order, part=self.dynamic, station_name="cut",
            step_order=1, quantity=1, status="pending",
        )
        self.stock = SheetStock.objects.create(
            material=self.material, width=Decimal("1830.0"),
            height=Decimal("3660.0"), quantity=2, created_by=self.manager,
        )
        self.base_record = CncFile.objects.get(
            barcode_key="panel.profile.1740x400", kind=CncFile.KIND_BASE)
        self.user = self.manager


class AutomaticProductionFlowTests(ManualQueueFlowMixin, TestCase):
    """بخش ۶ پرامپت — تسک آیندهٔ سازگار بدون بازکردن ادیتور از الگو استفاده
    می‌کند؛ دلتا برای هر تسک محاسبه می‌شود؛ منبع دست‌نخورده می‌ماند."""

    def test_api_saved_profile_automatically_resolves_future_task(self):
        self._login_manager()
        self._save_profile({"mode": "equal", "symmetric": True})   # ثبت واقعی از مسیر API
        self.client.logout()
        before_sha = self.base_record.sha256
        base_files = CncFile.objects.filter(kind=CncFile.KIND_BASE).count()
        record = resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1890.0"))
        self.assertEqual(record.kind, CncFile.KIND_DERIVED)
        self.assertEqual(record.source_file_id, self.base_record.pk)
        self.assertAlmostEqual(
            float(record.length_after) - float(record.length_before), 150.0, places=2)
        # فایل منبع دست نخورده و نسخهٔ پایهٔ جدیدی هم ثبت نشده است
        self.base_record.refresh_from_db()
        self.assertEqual(self.base_record.sha256, before_sha)
        self.assertEqual(
            CncFile.objects.filter(kind=CncFile.KIND_BASE).count(), base_files)

    def test_reprocessing_the_same_task_does_not_double_the_delta(self):
        self._login_manager()
        self._save_profile({"mode": "equal", "symmetric": True})
        self.client.logout()
        first = resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1890.0"))
        second = resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1890.0"))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.length_after, second.length_after)

    def test_different_target_lengths_get_task_specific_artifacts(self):
        self._login_manager()
        self._save_profile({"mode": "equal", "symmetric": True})
        self.client.logout()
        long = resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1890.0"))
        short = resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1790.0"))
        self.assertNotEqual(long.pk, short.pk)
        self.assertAlmostEqual(float(long.length_after) - float(long.length_before), 150.0, places=2)
        self.assertAlmostEqual(float(short.length_after) - float(short.length_before), 50.0, places=2)

    def test_proportional_profile_automatically_resizes_next_order(self):
        """مثال الزامی پرامپت با روش Proportional: الگو یک بار برای قطعه ذخیره
        می‌شود؛ سفارشِ هم‌طولِ پایه دلتای صفر و همان فایل پایه را می‌گیرد و
        سفارش بعدی با طول متفاوت — بدون هیچ تنظیم دستی — فایل مشتق با دلتای
        مخصوص همان سفارش می‌سازد. دلتا هرگز در الگو ذخیره نمی‌شود."""
        self._login_manager()
        self._save_profile({"mode": "proportional", "symmetric": True})
        self.client.logout()
        # سفارش اول: همان طول پایه ⇒ دلتای صفر ⇒ همان فایل پایه (بدون مشتق)
        same = resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1740.0"))
        self.assertEqual(same.pk, self.base_record.pk)
        # سفارش بعدی: طول متفاوت ⇒ مشتق خودکار از الگوی Proportional
        record = resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1890.0"))
        self.assertEqual(record.kind, CncFile.KIND_DERIVED)
        self.assertEqual(record.source_file_id, self.base_record.pk)
        self.assertEqual((record.params or {}).get("mode"), "proportional")
        self.assertAlmostEqual(
            float(record.length_after) - float(record.length_before), 150.0, places=2)

    def test_profile_of_one_part_is_invisible_to_unrelated_parts(self):
        """بند ۸-۴: الگوی یک قطعه به قطعهٔ/سفارش نامرتبط نشت نمی‌کند؛
        GET زنجیرهٔ دیگر «تعریف نشده» می‌دهد و resolve آن همان‌جا مسیر
        صف دستی (no_profile) می‌رود، نه استفاده از الگوی قطعهٔ دیگر."""
        self._login_manager()
        self._save_profile({"mode": "equal", "symmetric": True})   # الگوی زنجیرهٔ پایه/بلند
        other_base = Part.objects.create(
            material=self.material, name="پایهٔ زنجیرهٔ دیگر", length=Decimal("2000.0"),
            width=Decimal("600.0"), f3="panel.other.2000x600", routing_code="cnc",
        )
        other = Part.objects.create(
            material=self.material, name="دیگر", length=Decimal("2200.0"),
            width=Decimal("600.0"), f3="panel.other.2200x600", routing_code="cnc",
            base_part=other_base,
        )
        other_path = os.path.join(self.archive, "panel.other.2000x600.cnc")
        with open(other_path, "w", encoding="utf-8") as stream:
            stream.write(_sample_text().replace("N10G90G54", "N10G21G90G54", 1))
        register_base_file(other_path, other_base.f3, self.manager)
        self.client.logout()
        self._login_manager()   # نشست تازه — بازیابی الگو پس از ورود دوباره (بند ۸-۲)
        data = self._get_profile(other)
        self.assertFalse(data["approved"])   # الگوی زنجیرهٔ دیگر برای این قطعه دیده نمی‌شود
        with self.assertRaises(CncResolutionError) as ctx:
            resolve_cnc_file(other, self.user, effective_length=Decimal("2200.0"))
        self.assertEqual(_manual_reason_code(ctx.exception), "no_profile")

    def test_api_saved_profile_reaches_sheet_planner(self):
        self._login_manager()
        self._save_profile({"mode": "equal", "symmetric": True})
        self.client.logout()
        plans, issues, report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(report[0]["status"], "auto_adjusted")
        self.assertEqual(report[0]["file_kind"], "derived")
        self.assertEqual(report[0]["file_delta"], "+150.000")
        self.assertTrue(plans, "قطعهٔ مشتق باید به برنامه‌ریز ورق برسد")

    def test_missing_profile_falls_back_to_manual_queue_code(self):
        with self.assertRaises(CncResolutionError) as ctx:
            resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1890.0"))
        self.assertEqual(_manual_reason_code(ctx.exception), "no_profile")

    def test_unapproved_profile_is_never_used_automatically(self):
        CncPartProfile.objects.create(
            part=self.base, source_file=self.base_record,
            params={"mode": "equal", "symmetric": True, "axis": "length"},
            approved=False,
        )
        with self.assertRaises(CncResolutionError) as ctx:
            resolve_cnc_file(self.dynamic, self.user, effective_length=Decimal("1890.0"))
        self.assertEqual(_manual_reason_code(ctx.exception), "no_profile")
        self.assertIn("تأیید نشده", str(ctx.exception))


class GeometryGuardTests(ProfileApiFixtureMixin, TestCase):
    """بخش ۵ پرامپت — هندسهٔ پشتیبانی‌نشده امن مسدود می‌شود؛ ویژگی‌های مجاز
    (سوراخ‌ها) در خروجی مشتق حفظ می‌شوند."""

    def test_resize_preserves_hole_rows(self):
        from . import core
        text = _sample_text()
        probe = core.process(text, {"mode": "equal", "delta": 0})
        resized = core.process(text, {"mode": "equal", "delta": 150})
        self.assertTrue(probe["resizable"])
        self.assertEqual(len(probe["hole_rows"]), len(resized["hole_rows"]))
        self.assertTrue(resized["changed_lines"])

    def test_unresizable_base_file_is_rejected_before_any_stretch(self):
        material = Material.objects.create(name="MDF بد", thickness=Decimal("16.0"))
        part = Part.objects.create(
            material=material, name="بد", length=Decimal("1890.0"), width=Decimal("400.0"),
            f3="panel.profile.1890x400", routing_code="cnc",
        )
        archive = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, archive, ignore_errors=True)
        bad_path = os.path.join(archive, "panel.profile.1890x400.cnc")
        with open(bad_path, "w", encoding="utf-8") as stream:
            stream.write("this is not gcode")
        register_base_file(bad_path, part.f3, self.manager)
        CncPartProfile.objects.create(
            part=part, source_file=CncFile.objects.get(
                barcode_key="panel.profile.1890x400", kind=CncFile.KIND_BASE),
            params={"mode": "equal", "symmetric": True, "axis": "length"},
            approved=True,
        )
        with self.assertRaises(CncResolutionError) as ctx:
            resolve_cnc_file(part, self.manager, effective_length=Decimal("1990.0"))
        self.assertIn("قابل تغییر اندازه نیست", str(ctx.exception))
