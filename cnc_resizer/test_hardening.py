# -*- coding: utf-8 -*-
"""Batch-A hardening tests:
- outline selection by scoring instead of "last large block wins";
- early rejection of G91 / G20 / R-format arcs in core (before any geometry);
- derived-file metric self-check (length_after - length_before ~= delta).
"""
import os
import shutil
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import TestCase

from product.models import Material, Part, Product, ProductBOM, ProductCategory

from . import core
from .models import CncFile, CncPartProfile
from .services import CncResolutionError, register_base_file, resolve_cnc_file
from .tests import SAMPLE

# کانتور بستهٔ بدون قوس (امتیاز ۲) + بلوک raster بازِ بعد از آن (امتیاز ۰)؛
# با کد قدیمی بلوک دوم کانتور را قاپ می‌زد و «لبه‌های صاف بلند» پیدا نمی‌شد.
CONTOUR_THEN_RASTER = "\n".join([
    "G90",
    "G0 X0 Y0", "G1 X0 Y400", "G1 X200 Y400", "G1 X200 Y0", "G1 X0 Y0",
    "G0 X10 Y10",
    "G1 X200 Y10", "G1 X200 Y30", "G1 X10 Y30",
    "G1 X200 Y30", "G1 X200 Y150", "G1 X10 Y150",
    "M30", "",
])

RASTER_THEN_CONTOUR = "\n".join([
    "G90",
    "G0 X10 Y10",
    "G1 X200 Y10", "G1 X200 Y30", "G1 X10 Y30",
    "G1 X200 Y30", "G1 X200 Y150", "G1 X10 Y150",
    "G0 X0 Y0", "G1 X0 Y400", "G1 X200 Y400", "G1 X200 Y0", "G1 X0 Y0",
    "M30", "",
])


class OutlineScoringTests(TestCase):
    def test_closed_contour_beats_later_open_raster_block(self):
        levels, edge_lo, edge_hi = core.detect_geometry(CONTOUR_THEN_RASTER.splitlines())
        self.assertEqual((edge_lo, edge_hi), (0.0, 400.0))

    def test_closed_contour_beats_earlier_open_raster_block(self):
        levels, edge_lo, edge_hi = core.detect_geometry(RASTER_THEN_CONTOUR.splitlines())
        self.assertEqual((edge_lo, edge_hi), (0.0, 400.0))

    def test_resize_works_when_raster_follows_contour(self):
        result = core.process(CONTOUR_THEN_RASTER, {'delta': 10})
        self.assertTrue(result['resizable'])
        self.assertEqual(result['features'][0]['y'], 0)
        self.assertEqual(result['features'][-1]['y'], 400)
        self.assertAlmostEqual(result['length_after'] - result['length_before'], 10, places=2)

    def test_full_tie_still_prefers_the_last_block(self):
        # دو کانتور بستهٔ هم‌امتیاز و هم‌مساحت → رفتار تاریخی (آخرین بلوک).
        text = "\n".join([
            "G90",
            "G0 X0 Y0", "G1 X0 Y200", "G1 X200 Y200", "G1 X200 Y0", "G1 X0 Y0",
            "G0 X300 Y300", "G1 X300 Y500", "G1 X500 Y500", "G1 X500 Y300", "G1 X300 Y300",
            "M30", "",
        ])
        _, edge_lo, edge_hi = core.detect_geometry(text.splitlines())
        self.assertEqual((edge_lo, edge_hi), (300.0, 500.0))


class EarlySyntaxRejectionTests(TestCase):
    def test_g91_incremental_is_rejected_early(self):
        text = CONTOUR_THEN_RASTER.replace("G90", "G91", 1)
        with self.assertRaises(core.CncError) as ctx:
            core.process(text, {'delta': 10})
        self.assertIn('G91', str(ctx.exception))
        with self.assertRaises(core.CncError):
            core.analyze(text)

    def test_g20_inch_units_are_rejected_early(self):
        text = "G20\n" + CONTOUR_THEN_RASTER
        with self.assertRaises(core.CncError) as ctx:
            core.process(text, {'delta': 10})
        self.assertIn('G20', str(ctx.exception))
        with self.assertRaises(core.CncError):
            core.analyze(text)

    def test_r_format_arc_still_previews_but_is_flagged(self):
        # تصمیم محصول: R-format قید دیالکت است نه خراب‌کنندهٔ هندسه —
        # پیش‌نمایش ۲۰۰ می‌ماند و فقط خروجی ماشین در گیت دیالکت مسدود می‌شود.
        text = CONTOUR_THEN_RASTER.replace("M30", "G2 X110 Y10 R5\nM30", 1)
        result = core.process(text, {'delta': 10})
        self.assertTrue(result['resizable'])
        summary = core.program_summary(text.splitlines())
        self.assertEqual(summary['arc_format'], 'R')
        # تحلیل ویژگی هم بدون خطا کار می‌کند.
        inventory = core.analyze(text)
        self.assertTrue(inventory['ok'])

    def test_comments_mentioning_r_are_not_rejected(self):
        text = CONTOUR_THEN_RASTER.replace("G90", "G90 (tool R3 ; radius)", 1)
        result = core.process(text, {'delta': 10})
        self.assertTrue(result['resizable'])

    def test_plain_dialect_files_still_pass(self):
        with open(SAMPLE, "rb") as stream:
            text = stream.read().decode("utf-8-sig")
        result = core.process(text, {'delta': 10})
        self.assertTrue(result['resizable'])


class DerivedFileMetricCheckTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("cnc-manager-h", password="testpass")
        manager_group = Group.objects.create(name="h2")
        cls.manager.groups.add(manager_group)

    def _dynamic_part_with_base_file(self):
        category = ProductCategory.objects.create(name="CNC category h")
        product = Product.objects.create(category=category, name="CNC product h", base_price=1)
        material = Material.objects.create(name="MDF h", thickness=Decimal("16.0"))
        base = Part.objects.create(
            material=material, name="Panel h", length=Decimal("100.0"), width=Decimal("50.0"),
            f3="panelh.100x50", pname="CNC product h", routing_code="cnc",
        )
        ProductBOM.objects.create(product=product, part=base)
        dynamic = Part.objects.create(
            material=material, base_part=base, name=base.name,
            length=Decimal("120.0"), width=Decimal("50.0"),
            f3="panelh.120x50.item1", pname=base.pname, routing_code="cnc",
        )
        archive = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, archive, ignore_errors=True)
        source_path = os.path.join(archive, "panelh.100x50.cnc")
        with open(SAMPLE, "rb") as sample, open(source_path, "wb") as stream:
            stream.write(sample.read())
        source = register_base_file(source_path, base.f3, self.manager)
        return base, dynamic, source

    def test_resolver_blocks_derived_file_when_metric_delta_is_wrong(self):
        base, dynamic, source = self._dynamic_part_with_base_file()
        derived_dir = os.path.join(tempfile.gettempdir(), f"cnc_derived_h_{source.pk}")
        self.addCleanup(shutil.rmtree, derived_dir, ignore_errors=True)
        CncPartProfile.objects.create(
            part=base, source_file=source,
            params={'mode': 'equal', 'symmetric': True, 'axis': 'length'},
            approved=True, approved_by=self.manager,
        )
        real_process = core.process

        def lying_process(text, params):
            result = dict(real_process(text, params))
            result['length_after'] = result['length_before'] + float(params.get('delta', 0)) + 2.5
            return result

        with self.settings(CNC_DERIVED_DIR=derived_dir):
            with mock.patch('cnc_resizer.services.core.process', side_effect=lying_process):
                with self.assertRaises(CncResolutionError) as ctx:
                    resolve_cnc_file(dynamic, self.manager)
        self.assertIn('نمی‌خواند', str(ctx.exception))
        self.assertFalse(CncFile.objects.filter(kind=CncFile.KIND_DERIVED).exists())

    def test_resolver_still_creates_derived_file_when_metric_matches(self):
        base, dynamic, source = self._dynamic_part_with_base_file()
        derived_dir = os.path.join(tempfile.gettempdir(), f"cnc_derived_h_ok_{source.pk}")
        self.addCleanup(shutil.rmtree, derived_dir, ignore_errors=True)
        with self.settings(CNC_DERIVED_DIR=derived_dir):
            CncPartProfile.objects.create(
                part=base, source_file=source,
                params={'mode': 'equal', 'symmetric': True, 'axis': 'length'},
                approved=True, approved_by=self.manager,
            )
            record = resolve_cnc_file(dynamic, self.manager)
            self.assertTrue(os.path.isfile(record.path))
