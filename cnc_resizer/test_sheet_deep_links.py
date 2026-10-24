"""Deep links between the CNC sheet page and the CAD tool.

- GET /cnc/api/task-file/?task_id=N  → the exact file planning would use for
  that task; when resolution is blocked (no approved profile) the BASE file is
  served with ``resolvable: false`` so the operator can repair + register it.
- GET /cnc/api/sheet-program/<plan_id>/ → only the adapter-verified sheet
  gcode, fail-closed 409 otherwise.
- Layout provenance (file_kind / file_mode / file_delta) recorded per piece.
- Sheet page boxes deep-link to the tool with ``?task_id=``.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from product.models import Part, ProductionTask

from .models import CncFile, CncPartProfile
from .services import register_base_file
from .sheet_nesting import plan_cnc_with_report
from .test_manual_queue import ManualQueueFixtureMixin
from .tests import SAMPLE


class TaskFileDeepLinkTests(ManualQueueFixtureMixin, TestCase):
    def get(self, **params):
        return self.client.get(reverse('cnc_resizer:task_file'), params)

    def test_delta_zero_returns_base_file_as_resolvable(self):
        from cutting.specs import apply_override
        apply_override(self.task, {'length': '1740'}, self.user)
        response = self.get(task_id=self.task.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['meta']['file_kind'], 'base')
        self.assertTrue(data['meta']['resolvable'])
        self.assertFalse(data['meta']['manual_required'])
        self.assertEqual(data['meta']['effective_length'], '1740.0')
        with open(SAMPLE, encoding='utf-8-sig') as source:
            self.assertEqual(data['content'], source.read().replace('N10G90G54', 'N10G21G90G54', 1))

    def test_blocked_resolution_falls_back_to_base_and_flags_manual(self):
        # part.length=1890 vs base file 1740, no approved profile → planning
        # would route this to the manual queue; the tool gets the base file
        # plus the failure reason so the operator can fix it there.
        response = self.get(task_id=self.task.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertFalse(data['meta']['resolvable'])
        self.assertTrue(data['meta']['manual_required'])
        self.assertEqual(data['meta']['file_kind'], 'base')
        self.assertIn('الگوی CNC', data['meta']['failure_reason'])
        self.assertEqual(data['meta']['effective_length'], '1890.0')

    def test_non_cnc_part_is_rejected(self):
        plain_part = Part.objects.create(
            material=self.material, name='قطعه غیر CNC', length=Decimal('900.0'), width=Decimal('300.0'),
            f3='plain.part.900x300', routing_code='cut.paint', turn=False,
        )
        task = ProductionTask.objects.create(
            order=self.order, part=plain_part, station_name='cut', step_order=2,
            quantity=1, status='pending',
        )
        response = self.get(task_id=task.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn('مسیر CNC', json.loads(response.content)['error'])

    def test_missing_task_is_404(self):
        self.assertEqual(self.get(task_id=99999).status_code, 404)

    def test_invalid_id_is_400(self):
        self.assertEqual(self.get(task_id='abc').status_code, 400)

    def test_requires_operator_permission(self):
        outsider = User.objects.create_user('no-cnc', 'n@example.test', 'pw')
        self.client.force_login(outsider)
        self.assertEqual(self.get(task_id=self.task.pk).status_code, 403)


class SheetProgramApiTests(ManualQueueFixtureMixin, TestCase):
    def make_verified_plan(self):
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm',
                               CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router'):
            plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        return plans[0]

    def test_verified_plan_serves_gcode(self):
        plan = self.make_verified_plan()
        response = self.client.get(reverse('cnc_resizer:sheet_program', args=[plan.pk]))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['content'], plan.gcode)
        self.assertTrue(data['filename'].endswith('.nc'))
        self.assertEqual(data['meta']['pieces'], 1)

    def test_unverified_plan_is_fail_closed_409(self):
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)  # planned without adapter → no verified output
        response = self.client.get(reverse('cnc_resizer:sheet_program', args=[plans[0].pk]))
        self.assertEqual(response.status_code, 409)

    def test_cancelled_plan_is_409(self):
        plan = self.make_verified_plan()
        plan.status = 'cancelled'
        plan.save(update_fields=['status'])
        response = self.client.get(reverse('cnc_resizer:sheet_program', args=[plan.pk]))
        self.assertEqual(response.status_code, 409)

    def test_other_user_without_manage_is_403(self):
        plan = self.make_verified_plan()
        outsider = User.objects.create_user('no-cnc-2', 'n2@example.test', 'pw')
        self.client.force_login(outsider)
        response = self.client.get(reverse('cnc_resizer:sheet_program', args=[plan.pk]))
        self.assertEqual(response.status_code, 403)


class LayoutProvenanceTests(ManualQueueFixtureMixin, TestCase):
    def test_auto_adjusted_pieces_record_file_provenance(self):
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm',
                               CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router'):
            _plans, _issues, report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(report[0]['status'], 'auto_adjusted')
        self.assertEqual(report[0]['file_kind'], 'derived')
        self.assertEqual(report[0]['file_delta'], '+150.000')

        from .models import CncSheetPlan
        plan = CncSheetPlan.objects.get()
        piece = plan.layout['parts'][0]
        self.assertEqual(piece['file_kind'], 'derived')
        self.assertEqual(piece['file_delta'], '+150.000')
        self.assertEqual(piece['file_mode'], 'equal')

    def test_base_nested_pieces_record_base_kind(self):
        plain_part = Part.objects.create(
            material=self.material, name='صفحهٔ هم‌اندازه', length=Decimal('1740.0'), width=Decimal('400.0'),
            f3='panel.same.1740x400', routing_code='cnc.paint', turn=False,
        )
        ProductionTask.objects.create(
            order=self.order, part=plain_part, station_name='cut', step_order=2,
            quantity=1, status='pending',
        )
        archive = __import__('tempfile').TemporaryDirectory()
        self.addCleanup(archive.cleanup)
        import os
        same_path = os.path.join(archive.name, 'panel.same.1740x400.cnc')
        with open(SAMPLE, encoding='utf-8-sig') as source:
            text = source.read().replace('N10G90G54', 'N10G21G90G54', 1)
        with open(same_path, 'w', encoding='utf-8') as destination:
            destination.write(text)
        register_base_file(same_path, plain_part.f3, self.user)
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm',
                               CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router'):
            _plans, _issues, report = plan_cnc_with_report([self.order.pk], self.user)
        row = next(row for row in report if row['part_name'] == 'صفحهٔ هم‌اندازه')
        self.assertEqual(row['status'], 'nested')
        self.assertEqual(row['file_kind'], 'base')
        self.assertEqual(row['file_delta'], '0.000')


class SheetPageDeepLinkTests(ManualQueueFixtureMixin, TestCase):
    def test_boxes_link_to_cnc_tool_and_sheet_button(self):
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        with override_settings(CNC_RESIZER_IMPLICIT_UNITS='mm',
                               CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router'):
            plan_cnc_with_report([self.order.pk], self.user)
        page = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'?task_id={self.task.pk}')
        # بازطراحی: برچسب دکمهٔ نمای کل ورق در ابزار CAD به «نمای CAD» کوتاه شد (متن کامل در tooltip)
        self.assertContains(page, 'نمای CAD')
        self.assertContains(page, '?sheet_id=1')
        self.assertContains(page, 'cnc-part-box')
        self.assertContains(page, 'clickable')
        self.assertContains(page, 'فایل مشتق (الگوی تأییدشده)')
