"""فاز ۳.۱.۲ — preflight فقط-خواندنی فایل‌های CNC در پیش‌نمایش برنامه‌ریز.

قراردادهای کلیدی که این تست‌ها قفل می‌کنند:
- روی فایل واقعیِ سازگار با دیالکت، هیچ issue‌ای برنمی‌گردد؛
- preflight هیچ اثر جانبی ندارد: نه CncFile ثبت می‌شود نه CncPlanningIssue
  نوشته می‌شود و هیچ فایل مشتقی ساخته نمی‌شود (پیش‌نمایش GET است)؛
- نبودِ فایل پایه با reason_code=profile_failed گزارش می‌شود — همان نگاشتی
  که _plan_cnc از resolve_cnc_file می‌گیرد.
"""
import os
import shutil
import tempfile
from decimal import Decimal

from django.test import TestCase, override_settings

from product.models import Material, Part

from .models import CncFile, CncPlanningIssue
from .sheet_nesting import preflight_cnc_pieces

FLAGSHIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "t.1.1970x395.2set.cnc")


class PreflightCncPiecesTests(TestCase):
    def setUp(self):
        self.material = Material.objects.create(name="MDF preflight", thickness=Decimal("16.0"))

    def _part(self, f3="t.1.1970x395.2set", length="395.0", width="1970.0"):
        return Part.objects.create(
            material=self.material, name="پنل پیش‌نمایش", length=Decimal(length),
            width=Decimal(width), f3=f3, routing_code="cnc",
        )

    @staticmethod
    def _row(part, pieces=2):
        return {"part": part, "length": part.length, "width": part.width,
                "thickness": part.material.thickness, "pieces": pieces}

    def test_real_base_file_parses_clean_and_creates_nothing(self):
        part = self._part()
        with tempfile.TemporaryDirectory() as archive:
            shutil.copyfile(FLAGSHIP, os.path.join(archive, "t.1.1970x395.2set.cnc"))
            # فایل flagship کارگاه بدون G21 صریح است؛ همان قرارداد تست‌های
            # integration (واحد ضمنی mm) این‌جا هم برقرار است.
            with override_settings(CNC_SOURCE_DIR=archive, CNC_RESIZER_IMPLICIT_UNITS="mm"):
                issues = preflight_cnc_pieces([self._row(part)])
        self.assertEqual(issues, [])
        # قرارداد صادقانهٔ فقط-خواندنی: هیچ رکوردی ساخته نشده است.
        self.assertEqual(CncFile.objects.count(), 0)
        self.assertEqual(CncPlanningIssue.objects.count(), 0)

    def test_missing_base_file_maps_to_profile_failed_like_the_engine(self):
        part = self._part(f3="t.missing.file")
        with tempfile.TemporaryDirectory() as archive:
            with override_settings(CNC_SOURCE_DIR=archive):
                issues = preflight_cnc_pieces([self._row(part, pieces=3)])
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue["reason_code"], "profile_failed")
        self.assertIn("پیدا نشد", issue["message"])
        self.assertEqual(issue["pieces"], 3)
        self.assertEqual(CncFile.objects.count(), 0)

    def test_width_override_gate_fires_before_file_lookup(self):
        part = self._part()
        row = self._row(part)
        row["width"] = Decimal("1990.0")   # اوورراید عرض مؤثر
        issues = preflight_cnc_pieces([row])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["reason_code"], "width_changed")

    def test_thickness_override_gate_and_pieces_dedupe(self):
        material = Material.objects.create(name="MDF ضخامت متفاوت", thickness=Decimal("18.0"))
        part = Part.objects.create(
            material=material, name="پنل ضخامت", length=Decimal("395.0"),
            width=Decimal("1970.0"), f3="t.1.1970x395.2set", routing_code="cnc",
        )
        # مشخصاتِ ردیف اول در تجمیع per-part می‌ماند؛ دو تسکِ همان قطعه →
        # یک issue با جمع قطعه‌ها (پارس/جست‌وجوی فایل هم فقط یک‌بار).
        rows = [
            {"part": part, "length": part.length, "width": part.width,
             "thickness": Decimal("16.0"), "pieces": 2},
            self._row(part, pieces=1),   # ضخامت ۱۸ = متریال قطعه
        ]
        issues = preflight_cnc_pieces(rows)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["reason_code"], "thickness_changed")
        self.assertEqual(issues[0]["pieces"], 3)   # جمع قطعه‌های هر دو تسک
