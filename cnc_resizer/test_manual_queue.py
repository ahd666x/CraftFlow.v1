"""Stage-4 tests: CNC auto-adjust report, manual queue and manual resolution."""
import json
import os
import tempfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from cutting.models import SheetStock
from product.models import Customer, Material, Order, Part, ProductionTask

from .models import CncFile, CncPlanningIssue
from .services import register_base_file
from .sheet_nesting import MANUAL_REASON_MESSAGES, plan_cnc_with_report
from .tests import SAMPLE

# A minimal, dialect-valid workshop program whose cutting body measures
# exactly 1890mm in Y (G1 lines only; the trailing G0 retract is the dialect's
# required height-off move and the G53 lines form the park postamble).
MINIMAL_1890 = (
    "%\nN10G21G90G54\nN20G53 Z0\nN30T2M6\nN40 M141\nN50 M6T2\nN60 G43 H2\n"
    "N70G0X10Y10Z5.000M3 S16000\nN80G1Z-12.000F15000.0\n"
    "N90G1X10Y1900\nN100G1X100Y1900\nN110G1X100Y10\nN120G1X10Y10\n"
    "N130G0Z5.000M3 S16000\nN140G53Z0\nN150G53X0Y3950\nM30\n%\n"
)


class ManualQueueFixtureMixin:
    def setUp(self):
        self.user = User.objects.create_superuser('cnc-manual', 'm@example.test', 'pw')
        self.client.force_login(self.user)
        self.material = Material.objects.create(name='MDF صفه', thickness=Decimal('16.0'))
        customer = Customer.objects.create(name='مشتری صفه')
        self.order = Order.objects.create(customer=customer, status='planned')
        self.base_part = Part.objects.create(
            material=self.material, name='الگوی پایه', length=Decimal('1740.0'), width=Decimal('400.0'),
            f3='panel.manual.1740x400', routing_code='cnc.paint', turn=False,
        )
        # A production part derived from the base with +150mm length: the
        # auto-resize engine needs an approved profile for exactly this delta.
        self.part = Part.objects.create(
            material=self.material, name='صفه بلند', length=Decimal('1890.0'), width=Decimal('400.0'),
            f3='panel.manual.1890x400', routing_code='cnc.paint', turn=False, base_part=self.base_part,
        )
        self.task = ProductionTask.objects.create(
            order=self.order, part=self.part, station_name='cut', step_order=1,
            quantity=1, status='pending',
        )
        self.stock = SheetStock.objects.create(
            material=self.material, width=Decimal('1830.0'), height=Decimal('3660.0'),
            quantity=2, created_by=self.user,
        )
        self.archive = tempfile.TemporaryDirectory()
        self.addCleanup(self.archive.cleanup)
        base_path = os.path.join(self.archive.name, 'panel.manual.1740x400.cnc')
        with open(SAMPLE, encoding='utf-8-sig') as source:
            text = source.read().replace('N10G90G54', 'N10G21G90G54', 1)
        with open(base_path, 'w', encoding='utf-8') as destination:
            destination.write(text)
        register_base_file(base_path, self.base_part.f3, self.user)

    def plan_report(self):
        return plan_cnc_with_report([self.order.pk], self.user)


class ManualQueueRoutingTests(ManualQueueFixtureMixin, TestCase):
    def test_length_override_without_profile_routes_to_manual_queue(self):
        plans, issues, report = self.plan_report()
        self.assertEqual(plans, [])
        self.assertEqual(len(report), 1)
        row = report[0]
        self.assertEqual(row['status'], 'manual_required')
        self.assertEqual(row['reason_code'], 'no_profile')
        self.assertIn(MANUAL_REASON_MESSAGES['no_profile'][:30], row['message'])
        issue = CncPlanningIssue.objects.get(task=self.task, status='open')
        self.assertEqual(issue.details['stage'], 'manual_required')
        self.assertEqual(issue.details['reason_code'], 'no_profile')
        self.assertEqual(issue.details['quantity'], 1)

    def test_width_override_routes_to_manual_queue(self):
        from cutting.specs import apply_override
        apply_override(self.task, {'width': '450'}, self.user)
        _plans, _issues, report = self.plan_report()
        self.assertEqual(report[0]['status'], 'manual_required')
        self.assertEqual(report[0]['reason_code'], 'width_changed')

    def test_approved_profile_gives_auto_adjusted(self):
        from .models import CncPartProfile
        CncPartProfile.objects.create(
            part=self.base_part, source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        plans, issues, report = self.plan_report()
        self.assertEqual(
            [issue for issue in issues if issue.details.get('stage') == 'manual_required'], [])
        self.assertEqual(len(plans), 1)
        self.assertEqual(report[0]['status'], 'auto_adjusted')
        self.assertTrue(plans[0].layout['parts'][0]['spec_hash'])

    def test_base_file_never_used_silently_for_different_length(self):
        plans, issues, report = self.plan_report()
        self.assertEqual(plans, [])  # delta=150 without profile: no plan at all
        self.assertEqual(report[0]['status'], 'manual_required')


class ManualResolutionTests(ManualQueueFixtureMixin, TestCase):
    def open_issue(self):
        CncPlanningIssue.objects.create(
            task=self.task, order=self.order, part=self.part, mode='cnc_sheets',
            reason=MANUAL_REASON_MESSAGES['no_profile'],
            details={'stage': 'manual_required', 'reason_code': 'no_profile', 'quantity': 1},
            created_by=self.user,
        )

    def post_resolution(self, content, task_id=None):
        url = reverse('cnc_resizer:api_manual_resolution')
        return self.client.post(url, json.dumps({
            'task_id': task_id or self.task.pk, 'content': content,
        }).encode('utf-8'), content_type='application/json')

    def test_valid_manual_file_resolves_issue_and_enters_next_layout(self):
        self.open_issue()
        response = self.post_resolution(MINIMAL_1890)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        record = CncFile.objects.get(pk=payload['file_id'])
        self.assertEqual(record.kind, 'derived')
        self.assertEqual(record.params['mode'], 'manual')
        self.assertEqual(record.params['manual_for_task'], self.task.pk)
        self.assertEqual(record.params['target_length'], '1890.0')
        issue = CncPlanningIssue.objects.get(task=self.task)
        self.assertEqual(issue.status, 'resolved')
        self.assertIn('فایل دستی', issue.resolution_note)
        # Next planning run uses the registered manual file and nests the piece.
        plans, issues, report = self.plan_report()
        self.assertEqual(len(plans), 1)
        self.assertEqual(
            CncPlanningIssue.objects.filter(status='open', details__stage='manual_required').count(), 0)
        self.assertEqual(report[0]['status'], 'manual_resolved')

    def test_wrong_length_is_rejected_fail_closed(self):
        self.open_issue()
        wrong = MINIMAL_1890.replace('Y1900', 'Y1800')  # measured 1790 != 1890
        response = self.post_resolution(wrong)
        self.assertEqual(response.status_code, 400)
        self.assertIn('نمی\u200cخواند', response.json()['error'])
        self.assertFalse(CncFile.objects.filter(params__mode='manual').exists())
        self.assertTrue(CncPlanningIssue.objects.get(task=self.task, status='open') is not None)
        plans, _issues, report = self.plan_report()
        self.assertEqual(plans, [])
        self.assertEqual(report[0]['status'], 'manual_required')

    def test_bad_dialect_is_rejected(self):
        self.open_issue()
        response = self.post_resolution('%\nN10G20G90\nN20G0X1Y1\nM30\n%\n')
        self.assertEqual(response.status_code, 400)
        self.assertIn('دیالکت', response.json()['error'])

    def test_non_cnc_task_is_rejected(self):
        plain_part = Part.objects.create(
            material=self.material, name='دیواره', length=Decimal('400.0'), width=Decimal('300.0'),
            f3='wall.manual.400x300', routing_code='cut.paint', turn=True,
        )
        plain_task = ProductionTask.objects.create(
            order=self.order, part=plain_part, station_name='cut', step_order=2,
            quantity=1, status='pending',
        )
        response = self.post_resolution(MINIMAL_1890, task_id=plain_task.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn('مسیر CNC', response.json()['error'])

    def test_manual_file_requires_operator_permission(self):
        from django.contrib.auth.models import User
        outsider = User.objects.create_user('plain2', password='pw')
        self.client.force_login(outsider)
        response = self.post_resolution(MINIMAL_1890)
        self.assertEqual(response.status_code, 403)

    def test_resolve_cnc_file_returns_manual_record_for_exact_length(self):
        self.post_resolution(MINIMAL_1890)
        from .services import resolve_cnc_file
        record = resolve_cnc_file(self.part, self.user, effective_length=Decimal('1890.0'))
        self.assertEqual(record.kind, 'derived')
        self.assertEqual((record.params or {}).get('mode'), 'manual')
