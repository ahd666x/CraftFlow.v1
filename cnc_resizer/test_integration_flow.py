"""Phase-4 integration tests (P4): the full operator loop in one scenario.

Roadmap DoD «تست‌های یکپارچگی — سناریوهای Cut→CNC→Manual→Re-nest»: every
stage below goes through the SAME public entry point the operator uses —
the planner engine (``plan_cnc_with_report``, what the cutting planner POST
calls), the ``/cnc/manual/`` queue page, the manual-resolution API, the
Re-nest POST and the sheet-plans page. Phase-4 P3 additions are asserted on
the way: the creation-time ``preflight_report`` and the part-template →
planned-plan stale flag (advisory only — no gate changes anywhere).

Fixtures are borrowed from the phase-2.1 suite on purpose: a regression in
any earlier link of the chain fails here too.
"""
import json
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from product.models import Part

from .models import CncPartTemplate, CncPlanningIssue, CncSheetPlan
from .services import register_manual_cnc_file
from .sheet_nesting import plan_cnc_with_report
from .sheet_preview import SheetPreviewError, program_preflight
from .test_manual_page import SiteSettingsFixtureMixin
from .test_manual_queue import MINIMAL_1890, ManualQueueFixtureMixin


@override_settings(CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router')
class FullLoopIntegrationTests(SiteSettingsFixtureMixin, ManualQueueFixtureMixin, TestCase):
    def route_to_manual_queue(self):
        """Stage 1 — the planner engine routes the profile-less piece away."""
        plans, issues, report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(plans, [])
        self.assertEqual(report[0]['status'], 'manual_required')
        issue = CncPlanningIssue.objects.get(task=self.task, status='open')
        self.assertEqual(issue.details['stage'], 'manual_required')
        return issue

    def test_manual_queue_to_placed_piece_full_loop(self):
        self.route_to_manual_queue()
        # Stage 2 — the queue page lists the piece as «در انتظار اصلاح دستی».
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertContains(page, f'#{self.task.pk}')
        self.assertContains(page, 'در انتظار اصلاح دستی')
        # Stage 3 — the operator registers the hand-fixed file from the CAD tool.
        response = self.client.post(
            reverse('cnc_resizer:api_manual_resolution'),
            json.dumps({'task_id': self.task.pk, 'content': MINIMAL_1890}).encode('utf-8'),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertContains(page, 'آمادهٔ Nesting')
        rows = page.context['rows']
        self.assertEqual(len(rows), 1)
        issue_id = rows[0]['issue'].pk
        # Stage 4 — Re-nest directly from the queue.
        response = self.client.post(reverse('cnc_resizer:manual_renest'), {
            'issue_id': issue_id, 'next': '/cnc/manual/',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CncSheetPlan.objects.count(), 1)
        plan = CncSheetPlan.objects.get()
        self.assertTrue(plan.output_verified)
        # Phase-4 P3: the creation-time preflight report is honest and compact.
        preflight = plan.preflight_report
        self.assertTrue(preflight['ok'])
        self.assertEqual(preflight['pieces'], 1)
        self.assertEqual(preflight['tools'], [2])
        self.assertEqual(preflight['adapter'], 'minimal_xy_router')
        self.assertGreater(preflight['stats']['cut_mm'], 0)
        self.assertNotIn('paths', preflight)
        # Stage 5 — the sheet-plans page shows the placed plan and the queue row is gone.
        page = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertContains(page, 'CNC-')
        self.assertContains(page, 'پیش‌بررسی: سالم')
        queue_page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertNotContains(queue_page, f'#{self.task.pk}')

    def test_template_save_flags_planned_plan_and_page_shows_badge(self):
        register_manual_cnc_file(self.task, MINIMAL_1890, self.user)
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertFalse(plan.template_stale)
        template = CncPartTemplate.objects.create(
            part=self.part, name='آرایهٔ پیچ گوشه', payload=[],
            roles=[], source_bounds={}, created_by=self.user,
        )
        plan.refresh_from_db()
        self.assertTrue(plan.template_stale)
        self.assertEqual(plan.template_stale_info[0]['event'], 'saved')
        self.assertEqual(plan.template_stale_info[0]['name'], 'آرایهٔ پیچ گوشه')
        page = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertContains(page, 'قالب هندسی دستی تغییر کرده')
        self.assertContains(page, 'آرایهٔ پیچ گوشه')
        # Re-saving the SAME template refreshes its single entry (latest wins).
        for index in range(6):
            template.note = f'بازنگری {index}'
            template.save()
        plan.refresh_from_db()
        self.assertEqual(len(plan.template_stale_info), 1)
        self.assertEqual(plan.template_stale_info[0]['template_id'], template.pk)
        # The audit trail is capped at the last five distinct templates.
        for index in range(6):
            CncPartTemplate.objects.create(
                part=self.part, name=f'قالب اضافهٔ {index}', payload=[],
                roles=[], source_bounds={}, created_by=self.user,
            )
        plan.refresh_from_db()
        self.assertEqual(len(plan.template_stale_info), 5)
        # Deleting the template is also a change the operator should see.
        template.delete()
        plan.refresh_from_db()
        self.assertTrue(plan.template_stale)
        self.assertEqual(plan.template_stale_info[-1]['event'], 'deleted')

    def test_unrelated_part_template_does_not_flag(self):
        register_manual_cnc_file(self.task, MINIMAL_1890, self.user)
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        stranger = Part.objects.create(
            material=self.material, name='قطعهٔ بی‌ربط', length=Decimal('500.0'),
            width=Decimal('300.0'), f3='panel.stranger.500x300',
        )
        CncPartTemplate.objects.create(
            part=stranger, name='قالب دیگر', payload=[],
            roles=[], source_bounds={}, created_by=self.user,
        )
        plans[0].refresh_from_db()
        self.assertFalse(plans[0].template_stale)
        self.assertEqual(plans[0].template_stale_info, [])

    def test_done_plan_is_not_flagged(self):
        register_manual_cnc_file(self.task, MINIMAL_1890, self.user)
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        plan = plans[0]
        CncSheetPlan.objects.filter(pk=plan.pk).update(status='done')
        CncPartTemplate.objects.create(
            part=self.part, name='قالب پس از اجرا', payload=[],
            roles=[], source_bounds={}, created_by=self.user,
        )
        plan.refresh_from_db()
        self.assertFalse(plan.template_stale)


class BlockedOutputIntegrationTests(SiteSettingsFixtureMixin, ManualQueueFixtureMixin, TestCase):
    """No sheet adapter configured: pieces still nest, output stays blocked
    and the preflight report records the mute state instead of lying."""

    def test_plan_without_adapter_keeps_mute_preflight(self):
        register_manual_cnc_file(self.task, MINIMAL_1890, self.user)
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertFalse(plan.output_verified)
        self.assertEqual(plan.gcode, '')
        self.assertIsNone(plan.preflight_report['ok'])
        self.assertIn('پست‌پروسسور', plan.preflight_report['reason'])
        page = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertContains(page, 'پیش‌بررسی: —')


class ProgramPreflightUnitTests(TestCase):
    """Direct contract of the preflight helper (raise-path stays fail-closed)."""

    def test_garbage_program_raises_instead_of_lying(self):
        broken = MINIMAL_1890.replace('N90G1X10Y1900', 'N90G1X10Y1900B5')
        with self.assertRaises(SheetPreviewError):
            program_preflight(broken, 1830, 3660, [])
