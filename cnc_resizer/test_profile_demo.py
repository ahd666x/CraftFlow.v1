# ============================================================
# نمایش عملی end-to-end «الگوی تأییدشدهٔ تغییر اندازهٔ خودکار»
# (بخش ۱۱ پرامپت — قطعهٔ مستطیلی با طول پایهٔ ۲۰۰۰mm و تسک آیندهٔ ۲۲۰۰mm)
#
# اجرا: MEDIA_ROOT=... python manage.py test cnc_resizer.test_profile_demo -v 2
# خروجی واقعی (نه ساختگی) روی stdout چاپ می‌شود؛ پایگاه‌دادهٔ آزمون موقت است
# و پس از پایان تخریب می‌شود — هیچ دادهٔ واقعی دست نمی‌خورد.
#
# زنجیره: تسک تولید → طول هدف → الگوی تأییدشده (ثبت‌شده از مسیر HTTP ادیتور)
#   → هندسهٔ مشتق → استخراج قطعه → چیدمان ورق → خروجی ماشین با همان
#   پست‌پروسسور موجود (بدون دیالکت ساختگی).
# ============================================================
import os
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from cutting.models import SheetStock
from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CncFile, CncPartProfile
from .services import register_base_file, resolve_cnc_file
from .sheet_nesting import create_cnc_sheet_plans

# برنامهٔ کارگاهی معتبرِ دیالکت: مستطیل ۲۰۰۰mm در Y با دو سوراخ ۴mm
# (سوراخ‌ها فاصله‌های میانی را می‌سازند تا موتور توزیع دلتا داشته باشد).
DEMO_2000 = (
    "%\nN10G21G90G54\nN20G53 Z0\nN30T2M6\nN40 M141\nN50 M6T2\nN60 G43 H2\n"
    "N70G0X0Y0Z5.000M3 S16000\nN80G1Z-12.000F15000.0\n"
    "N90G1X0Y2000\nN100G1X150Y2000\nN110G1X150Y0\nN120G1X0Y0\n"
    "N130G0Z5.000M3 S16000\n"
    "N140G0X60Y498M3 S16000\nN150G1Z-12.000F15000.0\n"
    "N160G1X60Y502\nN170G1X64Y502\nN180G1X64Y498\nN190G1X60Y498\n"
    "N200G0Z5.000M3 S16000\n"
    "N210G0X60Y1498M3 S16000\nN220G1Z-12.000F15000.0\n"
    "N230G1X60Y1502\nN240G1X64Y1502\nN250G1X64Y1498\nN260G1X60Y1498\n"
    "N270G0Z5.000M3 S16000\n"
    "N280G53Z0\nN290G53X0Y3950\nM30\n%\n"
)


def _p(label):
    print(f"  DEMO | {label}", flush=True)


class ProfileEndToEndDemo(TestCase):
    def test_demo_baseline_2000_future_task_2200(self):
        _p("=== نمایش end-to-end: پایه ۲۰۰۰mm → تسک آیندهٔ ۲۲۰۰mm ===")

        # ---- ۱) دادهٔ کارگاه: قطعهٔ پایهٔ ۲۰۰۰mm + قطعهٔ سفارشِ ۲۲۰۰mm
        manager = User.objects.create_user("demo-profile-manager", password="pw")
        manager.groups.add(Group.objects.get_or_create(name="2")[0])
        material = Material.objects.create(name="MDF نمایش", thickness=Decimal("16.0"))
        base = Part.objects.create(
            material=material, name="قطعهٔ پایهٔ نمایش", length=Decimal("2000"),
            width=Decimal("400"), f3="demo.profile.2000x400", routing_code="cnc",
        )
        future = Part.objects.create(
            material=material, name="قطعهٔ سفارش نمایش", length=Decimal("2200"),
            width=Decimal("400"), f3="demo.profile.2200x400", routing_code="cnc",
            base_part=base,
        )
        order = Order.objects.create(
            customer=Customer.objects.create(name="مشتری نمایش"), status="planned")
        ProductionTask.objects.create(
            order=order, part=future, station_name="cut", step_order=1,
            quantity=1, status="pending",
        )
        SheetStock.objects.create(
            material=material, width=Decimal("1830"), height=Decimal("3660"),
            quantity=2, created_by=manager,
        )
        _p(f"قطعهٔ پایه: {base.f3} — Part.length = 2000mm | قطعهٔ تسک آینده: "
           f"{future.f3} — Part.length = 2200mm")

        # ---- ۲) فایل پایهٔ واقعی (مستطیل ۲۰۰۰mm + دو سوراخ) در آرشیو ثبت می‌شود
        archive = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, archive, ignore_errors=True)
        base_path = os.path.join(archive, "demo.profile.2000x400.cnc")
        with open(base_path, "w", encoding="utf-8") as stream:
            stream.write(DEMO_2000)
        base_record = register_base_file(base_path, base.f3, manager)
        self.assertTrue(base_record.resizable)
        _p(f"فایل پایه ثبت شد: #{base_record.pk} «{base_record.barcode_key}» "
           f"span(Y) = {base_record.length_before}mm — resizable=True")

        # ---- ۳) ثبت الگو از مسیر واقعی HTTP ادیتور (همان API که دکمهٔ
        #         «تأیید روش تغییر طول برای این قطعه» صدا می‌زند)
        client = Client()
        client.force_login(manager)
        profile_url = reverse("cnc_resizer:part_profile")
        got = client.get(f"{profile_url}?part_id={future.pk}").json()
        self.assertEqual(got["source_id"], base_record.pk)
        params = {"mode": "equal", "symmetric": True, "axis": "length",
                  "enabled": [True] * 3}
        saved = client.post(
            f"{profile_url}?part_id={future.pk}",
            data=f'{{"part_id": {future.pk}, "source_id": {base_record.pk}, '
                 f'"params": {{"mode": "equal", "symmetric": true, "axis": "length", "enabled": [true, true, true]}}}}',
            content_type="application/json",
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        profile = CncPartProfile.objects.get(part=future)
        _p(f"الگو از API ادیتور ثبت و تأیید شد: #{profile.pk} — mode=equal، "
           f"symmetric=True، enabled=۳/۳ — delta در الگو ذخیره نمی‌شود "
           f"(کلیدهای ذخیره‌شده: {sorted(profile.params)})")

        # ---- ۴) تسک آینده بدون بازکردن ادیتور: دلتا از طول هدف ساخته می‌شود
        record = resolve_cnc_file(future, manager, effective_length=Decimal("2200"))
        task_delta = Decimal("2200") - Decimal("2000")
        measured = record.length_after - record.length_before
        _p(f"تسک آینده (طول مؤثر ۲۲۰۰): دلتای محاسبه‌شده = {task_delta:+}mm "
           f"(= ۲۲۰۰ − ۲۰۰۰) — الگوی ذخیره‌شده خودش اعمال شد")
        _p(f"فایل مشتق: #{record.pk} کلید «{record.barcode_key}» mode={record.params.get('mode')} "
           f"طول {record.length_before} → {record.length_after}mm (اندازه‌گیری‌شده: {measured:+}mm)")
        _p(f"مسیر آرتیفکت: {record.path}")
        self.assertEqual(record.kind, CncFile.KIND_DERIVED)
        self.assertEqual(record.source_file_id, base_record.pk)
        self.assertEqual(record.params.get("mode"), "equal")
        self.assertAlmostEqual(float(measured), 200.0, places=2)

        # ---- ۵) فایل منبع دست‌نخورده + پردازش دوباره = بدون دوباره‌دلتا
        base_record.refresh_from_db()
        again = resolve_cnc_file(future, manager, effective_length=Decimal("2200"))
        _p(f"فایل پایهٔ منبع دست‌نخورده (sha256 همان «{base_record.sha256[:16]}…»)؛ "
           f"پردازش دوبارهٔ همین تسک → همان آرتیفکت #{again.pk} (کلید content-addressed) "
           f"— طول {again.length_after}mm، دلتای اضافه = 0")
        self.assertEqual(again.pk, record.pk)

        # ---- ۶) چیدمان ورق + خروجی ماشین با همان پست‌پروسسور موجود
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm",
                               CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router"):
            plans, issues = create_cnc_sheet_plans([order.pk], manager)
        self.assertEqual(issues, [], issues)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        placed_parts = plan.layout.get("parts") or []
        first_part = placed_parts[0] if placed_parts else {}
        _p(f"برنامه‌ریزی ورق: {len(plans)} برنامه ساخته شد — وضعیت {plan.status}، "
           f"ورق {plan.width}×{plan.height}mm، قطعه‌های چیده‌شده = {len(placed_parts)}"
           + (f" — قطعهٔ اول: «{first_part.get('f3')}» در ({first_part.get('x')}, {first_part.get('y')}) "
              f"با file_kind={first_part.get('file_kind')} و file_delta={first_part.get('file_delta')}"
              if first_part else ""))
        _p(f"خروجی ماشین با همان کنترلر موجود (minimal_xy_router): "
           f"output_verified={plan.output_verified}، adapter={plan.layout.get('adapter')}، "
           f"طول G-code = {len(plan.gcode)} کاراکتر، شروع با «{plan.gcode[:20]!r}»")
        self.assertTrue(plan.output_verified)
        self.assertEqual(plan.layout.get("adapter"), "minimal_xy_router")
        self.assertTrue(plan.gcode.startswith("%"))

        _p("=== پایان نمایش — همهٔ اعداد بالا از اجرای واقعی همین آزمون است ===")
