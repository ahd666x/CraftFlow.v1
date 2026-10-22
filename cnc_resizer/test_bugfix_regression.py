"""Regression coverage for the operator bug-report batch that followed
prompt-2 phase «د». One focused test per fixed defect:

* part pairing by contour BLOCK membership (was: sort-index pairing that
  broke as soon as an edit flipped the ``(y_min, x_min)`` order);
* ``set_gap`` composed against a part moved EARLIER in the same request,
  and a hard reject when the anchor was stretched (non-rigid bbox);
* ``engine_preview``/``process`` tool-table injection parity;
* ``normalize_units`` never changes the line count (no G90 anchor -> no
  injection), explicit G20 is not "unit-less", G91.1 is not G91;
* ``CncControllerProfile.approved`` is wired into the machine-output gate;
* view-only geometry uses the same tool table as the finished-size report;
* serializer copy lines always terminated (unterminated last source line);
* template audit: ``created_by`` survives updates; part-level ops are
  rejected in *parametric* part templates at save time;
* admin registration and the HTML-vs-JSON 403 split.
"""
import json
import unittest

from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import controller, core
from .engine.parser import parse_program
from .engine.parametric import TransformError
from .engine.parts import detect_parts
from .engine.part_edits import edits_part_program
from .engine.recognition import recognize
from .engine.serializer import serialize_candidate
from .engine.service import edits_program
from .test_tooltable import build_kerf_file
from .test_part_templates import BASE_FILE as PART_TEMPLATE_FILE

THREE_PARTS_TEXT = "\n".join([
    "%",
    "N10G90G54",
    "N20G53 Z0",
    "N30T3M6",
    "N40 M141",
    "N50 M6T3",
    "N60 G43 H3",
    "N70G0X0.000Y10.000Z5.000M3 S16000",
    "N80G1Z-16.000F10000.0",
    "N90G1X100.000",
    "N100G1Y110.000",
    "N110G1X0.000",
    "N120G1Y10.000",
    "N130G0Z5.000M3 S16000",
    "N140G0X0.000Y310.000M3 S16000",
    "N150G1Z-16.000F10000.0",
    "N160G1X100.000",
    "N170G1Y410.000",
    "N180G1X0.000",
    "N190G1Y310.000",
    "N200G0Z5.000M3 S16000",
    "N210G0X0.000Y510.000M3 S16000",
    "N220G1Z-16.000F10000.0",
    "N230G1X100.000",
    "N240G1Y610.000",
    "N250G1X0.000",
    "N260G1Y510.000",
    "N270G0Z5.000M3 S16000",
    "N280G0X0.000Y0.000M3 S16000",
    "N290G53Z0",
    "N300G53X0Y3950",
    "N310M30",
    "%",
]) + "\n"


def _three_parts_by_y():
    program = parse_program(THREE_PARTS_TEXT, filename="three.cnc")
    detection = detect_parts(program)
    ordered = sorted(detection["parts"], key=lambda part: part["bbox"]["y_min"])
    return ordered[0], ordered[1], ordered[2]


class PartPairingByBlockMembershipTests(SimpleTestCase):
    """Bug 1: the collision gate must pair source/runtime parts by contour
    block membership, not by the (y_min, x_min) sort index."""

    def test_order_flipping_move_reports_the_real_colliding_pair(self):
        low, _middle, top = _three_parts_by_y()
        # move the top part 450mm down: it lands ON the low part (58mm
        # finished-bbox overlap) and its y_min crosses the low part's, so the
        # runtime sort order flips. The gate must flag {low, top} — with
        # index pairing it wrongly flagged {low, middle} (two untouched,
        # 200mm-apart parts) and missed the real pair entirely.
        result = edits_part_program(THREE_PARTS_TEXT, {"edits": [
                {"op": "move_part", "part_id": top["id"], "dx": 0.0, "dy": -450.0},
            ]}, filename="three.cnc")
        self.assertFalse(result["validation"]["passed"])
        flagged = {frozenset(item["parts"]) for item in result["collision_check"]["flagged"]}
        self.assertIn(frozenset({low["id"], top["id"]}), flagged)
        self.assertNotIn(frozenset({low["id"], _middle["id"]}), flagged)


class SetGapCompositionTests(SimpleTestCase):
    """Bug 12: set_gap must measure from the anchor's CURRENT position when
    the anchor was moved earlier in the same request, and must reject a
    stretched anchor (its bbox change is not a rigid translation)."""

    def test_set_gap_after_move_composes_from_new_position(self):
        _low, middle, top = _three_parts_by_y()
        # move the top part down 50mm (clear of the middle part), then demand
        # a 20mm finished gap between them. Composed from the NEW position the
        # middle part moves up 22mm; measured from the stale source position
        # it would move 72mm and collide with the moved part.
        result = edits_part_program(THREE_PARTS_TEXT, {"edits": [
                {"op": "move_part", "part_id": top["id"], "dx": 0.0, "dy": -50.0},
                {"op": "set_gap", "part_a": top["id"], "part_b": middle["id"],
                 "axis": "y", "gap": 20.0},
            ]}, filename="three.cnc")
        self.assertTrue(result["validation"]["passed"],
                        msg=str(result["validation"]["errors"]))
        runtime = {tuple(part["contour_block_ids"]): part
                   for part in result["resized"]["parts"]}
        middle_rt = runtime[tuple(middle["contour_block_ids"])]
        top_rt = runtime[tuple(top["contour_block_ids"])]
        self.assertAlmostEqual(middle_rt["bbox"]["y_max"], 432.0, places=1)  # 410 + 22
        self.assertAlmostEqual(top_rt["bbox"]["y_min"], 460.0, places=1)
        finished_gap = top_rt["bbox"]["y_min"] - middle_rt["bbox"]["y_max"] - 8.0
        self.assertAlmostEqual(finished_gap, 20.0, places=1)

    def test_set_gap_with_stretched_anchor_is_rejected(self):
        _low, middle, top = _three_parts_by_y()
        with self.assertRaises(TransformError) as ctx:
            edits_part_program(THREE_PARTS_TEXT, {"edits": [
                    {"op": "stretch_part", "part_id": top["id"], "axis": "y", "delta": 50.0},
                    {"op": "set_gap", "part_a": top["id"], "part_b": middle["id"],
                     "axis": "y", "gap": 20.0},
                ]}, filename="three.cnc")
        self.assertIn("کشیده شده", str(ctx.exception))


class NormalizeUnitsSafetyTests(SimpleTestCase):
    """Bug 13: unit normalization must never change the line count and must
    treat explicit inch files as declared (not unit-less)."""

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm")
    def test_explicit_inch_file_gets_no_g21(self):
        text = "%\nG20\nG90 G54\nG0 X0 Y0\n%\n"
        normalized, injected = controller.normalize_units(text)
        self.assertFalse(injected)
        self.assertEqual(normalized, text)
        self.assertNotIn("G21", normalized.replace("(x)", ""))

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm")
    def test_missing_g90_anchor_inserts_no_line(self):
        text = "%\nG54\nG0 X0 Y0\nG1 X10\n%\n"
        normalized, injected = controller.normalize_units(text)
        self.assertFalse(injected)
        self.assertEqual(len(normalized.splitlines()), len(text.splitlines()))

    @override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm")
    def test_g90_anchor_substitution_keeps_line_count(self):
        text = "%\nN10G90G54\nG0 X0 Y0\n%\n"
        normalized, injected = controller.normalize_units(text)
        self.assertTrue(injected)
        self.assertEqual(len(normalized.splitlines()), len(text.splitlines()))
        self.assertIn("G21", normalized.splitlines()[1])

    def test_g21_inside_comment_does_not_count(self):
        text = "%\n(unit: not G21 here)\nN10G90G54\nG0 X0 Y0\n%\n"
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS="mm"):
            normalized, injected = controller.normalize_units(text)
        self.assertTrue(injected)
        self.assertEqual(len(normalized.splitlines()), len(text.splitlines()))

    def test_g91_point_one_is_not_incremental_mode(self):
        core._reject_unsupported_syntax(["G91.1", "G90"])  # must not raise
        with self.assertRaises(core.CncError):
            core._reject_unsupported_syntax(["G91"])


class ControllerApprovalGateTests(TestCase):
    """Bug 5: an active, unapproved CncControllerProfile must block the
    machine-output path; approving it (or having no row) must not."""

    @classmethod
    def setUpTestData(cls):
        from .models import CncControllerProfile
        cls.model = CncControllerProfile

    @override_settings(CNC_RESIZER_SHEET_ADAPTER="minimal_xy_router",
                       CNC_RESIZER_IMPLICIT_UNITS="mm")
    def test_unapproved_active_profile_blocks_output(self):
        profile = self.model.objects.create(key="audit-router", manufacturer="X", model="Y",
                                            adapter_key="minimal_xy_router", active=True,
                                            approved=False)
        blocker = controller.unapproved_controller_blocker()
        self.assertIsNotNone(blocker)
        self.assertIn("تأیید", blocker)
        with self.assertRaises(controller.ControllerPostprocessError) as ctx:
            controller.postprocess_resize(THREE_PARTS_TEXT, THREE_PARTS_TEXT)
        self.assertIn("تأیید", str(ctx.exception))
        profile.approved = True
        profile.save(update_fields=["approved"])
        self.assertIsNone(controller.unapproved_controller_blocker())
        output = controller.postprocess_resize(THREE_PARTS_TEXT, THREE_PARTS_TEXT)
        self.assertEqual(output["adapter_key"], "minimal_xy_router")

    def test_inactive_or_unbound_profile_does_not_block(self):
        self.model.objects.create(key="idle-router", manufacturer="X", model="Y",
                                  adapter_key="minimal_xy_router", active=False,
                                  approved=False)
        self.model.objects.create(key="other-router", manufacturer="X", model="Y",
                                  adapter_key="some_other_adapter", active=True,
                                  approved=False)
        self.assertIsNone(controller.unapproved_controller_blocker())


class ProcessToolMapWithoutParamsTests(TestCase):
    """Bug 3: the process view must inject the DB tool table even when the
    client sends no ``params`` object at all."""

    @classmethod
    def setUpTestData(cls):
        from .models import ToolSlot
        cls.manager = User.objects.create_user("noparams-manager", password="testpass")
        cls.manager.groups.add(Group.objects.create(name="2"))
        ToolSlot.objects.create(slot=3, label="فرز", dia=6)

    def test_process_without_params_still_uses_db_table(self):
        self.client.login(username="noparams-manager", password="testpass")
        payload = {"content": build_kerf_file(), "delta": 0}   # no params object
        resp = self.client.post(reverse("cnc_resizer:process"), data=json.dumps(payload),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        for hole in data["resized"]["holes"]:
            self.assertEqual(hole["dia"], 10.0)  # T3 = 6 from the DB, not the 8mm default
        self.assertIn("T3 = 6mm", "\n".join(data["warnings"]))


class ViewOnlyToolMapTests(SimpleTestCase):
    """Bug 14: view-only geometry must compensate holes with the same tool
    table the finished-size report uses."""

    def test_view_only_holes_use_tool_table(self):
        text = build_kerf_file()
        lines, eol = core.split_lines(text)
        result = core.view_only_result(text, lines, eol, 8.0, "تست", tool_map={3: 6.0})
        self.assertTrue(result["holes" if "holes" in result else "original"])
        holes = result["original"]["holes"]
        self.assertTrue(holes)
        for hole in holes:
            if hole.get("tool") == 3:
                self.assertEqual(hole["tool_dia_used"], 6.0)
        self.assertEqual(result["finished_tool_dia"], 6.0)


class SerializerTrailingNewlineTests(SimpleTestCase):
    """Bug 16: copy lines of an array edit are new text and must always carry
    the file's EOL, even when the source's last line is unterminated."""

    ROUNDED_RECT = "\n".join([
        "G21 G90 G17", "G0 X0 Y0 Z5", "G1 Z-3",
        "G1 X120 Y0",
        "G3 X130 Y10 I0 J10",
        "G1 Y190",
        "G3 X120 Y200 I-10 J0",
        "G1 X0 Y200",
        "G3 X-10 Y190 I0 J-10",
        "G1 Y10",
        "G3 X0 Y0 I10 J0", "",
    ])

    def test_array_copy_lines_terminated_without_trailing_newline(self):
        text = self.ROUNDED_RECT.rstrip("\n")           # last line unterminated
        program = parse_program(text, filename="rect.cnc")
        features = recognize(program)["features"]
        contour = next(f for f in features if f.get("bounds"))
        preview = edits_program(text, {"edits": [
            {"op": "array", "feature_id": contour["id"], "axis": "x", "count": 1, "pitch": 160.0},
        ]}, filename="rect.cnc")
        self.assertTrue(preview["validation"]["passed"],
                        msg=str(preview["validation"]["errors"]))
        self.assertTrue(preview.get("block_copies"))
        draft = serialize_candidate(text, preview)
        source_lines = text.splitlines()
        out_lines = draft["text"].splitlines()
        self.assertEqual(len(out_lines),
                         len(source_lines) + draft["inserted_line_count"])
        self.assertTrue(draft["text"].endswith("\n"))
        # no glued line: every output line is a complete G-code line
        for line in out_lines:
            self.assertTrue(line.strip())
            self.assertFalse(line.rstrip().endswith("G3 X0 Y0 I10 J0G0"))


class EditTemplateAuditTests(TestCase):
    """Bug 18: updating a template must not hand ``created_by`` to whoever
    edited it last — the original author stays auditable."""

    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user("tpl-author", password="testpass")
        cls.editor = User.objects.create_user("tpl-editor", password="testpass")
        group = Group.objects.create(name="2")
        cls.author.groups.add(group)
        cls.editor.groups.add(group)

    def _post(self, name, edits):
        return self.client.post(reverse("cnc_resizer:edit_templates"),
                                data=json.dumps({"name": name, "edits": edits}),
                                content_type="application/json")

    def test_update_keeps_original_creator(self):
        from .models import EditTemplate
        edits = [{"op": "shift", "feature_id": "feature-B2", "dx": 1, "dy": 0}]
        self.client.login(username="tpl-author", password="testpass")
        self.assertEqual(self._post("قالب حسابرسی", edits).status_code, 200)
        self.client.login(username="tpl-editor", password="testpass")
        self.assertEqual(self._post("قالب حسابرسی", edits).status_code, 200)
        template = EditTemplate.objects.get(name="قالب حسابرسی")
        self.assertEqual(template.created_by.username, "tpl-author")


class PartTemplatePartOpsRejectedTests(TestCase):
    """Bug 10: the parametric part template captures FEATURE roles only —
    saving part-level ops must fail at save time with a clear reason instead
    of producing a 0-role template that breaks at apply time."""

    @classmethod
    def setUpTestData(cls):
        from product.models import Material, Part
        from .models import CncPartProfile, CncFile
        cls.operator = User.objects.create_user("pt-operator", password="testpass")
        cls.operator.groups.add(Group.objects.create(name="2"))
        cls.part = Part.objects.create(
            material=Material.objects.create(name="MDF-reg", thickness=16),
            name="درب رگرسیون", length=1090, width=450, pname="کابینت",
            routing_code="CNC",
        )
        base = CncFile.objects.create(barcode_key="T16-reg", path="/tmp/t.16.cnc", sha256="b" * 64)
        CncPartProfile.objects.create(part=cls.part, source_file=base)

    def test_move_part_edit_is_rejected_at_save_time(self):
        self.client.login(username="pt-operator", password="testpass")
        response = self.client.post(
            reverse("cnc_resizer:part_templates"),
            data=json.dumps({"name": "قالب قطعه‌ای", "part_id": self.part.pk,
                             "content": PART_TEMPLATE_FILE, "filename": "t.16.cnc",
                             "edits": [{"op": "move_part", "part_id": "part-abc", "dx": 5, "dy": 0}]}),
            content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("قالب قطعه", response.json()["error"])

    def test_feature_edits_still_save(self):
        from .test_part_templates import SHIFT_EDITS
        self.client.login(username="pt-operator", password="testpass")
        response = self.client.post(
            reverse("cnc_resizer:part_templates"),
            data=json.dumps({"name": "قالب ویژگی‌محور", "part_id": self.part.pk,
                             "content": PART_TEMPLATE_FILE, "filename": "t.16.cnc",
                             "edits": SHIFT_EDITS}),
            content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreater(response.json()["role_count"], 0)


class AdminRegistrationTests(TestCase):
    """Bug 5a: the admin module registers every CNC model again — profile
    approval has a real surface (README + register_cnc_controller promise)."""

    def test_all_cnc_models_are_registered(self):
        from django.contrib import admin
        from . import admin as cnc_admin  # noqa: F401 — registration side effect
        from .models import (CncControllerProfile, CncEngineeringProfile, CncFile,
                             CncPartProfile, CncPartTemplate, CncPlanningIssue,
                             CncSheetPlan, EditTemplate, ToolSet, ToolSlot)
        registered = {model.__name__ for model in admin.site._registry
                      if model._meta.app_label == "cnc_resizer"}
        expected = {cls.__name__ for cls in (CncControllerProfile, CncEngineeringProfile,
                                             CncFile, CncPartProfile, CncPartTemplate,
                                             CncPlanningIssue, CncSheetPlan, EditTemplate,
                                             ToolSet, ToolSlot)}
        self.assertEqual(expected - registered, set())


class HtmlForbiddenTests(TestCase):
    """Bug 20: server-rendered CNC views answer a denied user with a real 403
    page, not a JSON body meant for fetch()."""

    @classmethod
    def setUpTestData(cls):
        cls.outsider = User.objects.create_user("no-cnc-access", password="testpass")

    def test_sheet_plans_denied_gets_html_403(self):
        self.client.login(username="no-cnc-access", password="testpass")
        response = self.client.get(reverse("cnc_resizer:sheet_plans"))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response["Content-Type"].startswith("text/html"))

    def test_api_views_keep_json_403(self):
        self.client.login(username="no-cnc-access", password="testpass")
        response = self.client.get(reverse("cnc_resizer:tools"))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
