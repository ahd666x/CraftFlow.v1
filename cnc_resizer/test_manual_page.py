"""Phase 2.1 tests: the /cnc/manual/ queue page, Re-nest and part signals.

Contract under test:
- The queue lists open manual_required issues plus recently resolved ones
  whose pieces are still unplaced («آمادهٔ Nesting»); rows disappear once the
  planner actually places the pieces.
- Signals are read-only reports of state created by other audited flows:
  a registered manual file on disk, an approved resize profile, a saved
  parametric part template. Rendering the queue never mutates anything.
- Re-nest runs the SAME planner engine (plan_cnc_with_report) for the row's
  order and resolves the task's open manual_required issues only on real
  success; a still-blocked task keeps its issue open (fail-closed).
"""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import CncFile, CncPartProfile, CncPartTemplate, CncPlanningIssue, CncSheetPlan
from .services import register_manual_cnc_file
from .sheet_nesting import plan_cnc_with_report
from .test_manual_queue import MINIMAL_1890, ManualQueueFixtureMixin


class SiteSettingsFixtureMixin:
    """Keep the storefront custom 404 page renderable inside tests.

    The 404 template dereferences ``site_settings`` eagerly, and the storefront
    middleware caches the SiteSettings object across tests while the context
    processor re-queries the (rolled-back) test DB. A populated row in every
    test makes the 404 page deterministic; inert for CNC logic.
    """

    def setUp(self):
        from storefront.models import SiteSettings
        SiteSettings.get_settings()
        super().setUp()


class ManualQueuePageTests(ManualQueueFixtureMixin, TestCase):
    def route_to_queue(self):
        plans, issues, report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(plans, [])
        self.assertEqual(report[0]['status'], 'manual_required')
        return CncPlanningIssue.objects.get(task=self.task, status='open')

    def test_routed_issue_is_listed_with_deep_link(self):
        self.route_to_queue()
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'#{self.task.pk}')
        self.assertContains(page, '?task_id=')
        # بازبینی دور ۳: لینک عمیق با {% url %} ساخته می‌شود (index روی /cnc/ سوار
        # است، پس خروجی رندر همان است) — هاردکد در «منبع» قالب حذف شده.
        self.assertContains(page, 'در انتظار اصلاح دستی')
        # ستون «علت» حذف شده است؛ اما بازبینی دور ۳ (بند ۸): دلیل ورود به صف
        # حالا در سلول وضعیت صریح رندر می‌شود تا اپراتور بداند چرا اینجاست.
        self.assertContains(page, 'دلیل ورود:')
        self.assertContains(page, 'الگوی خودکار تأییدشده ثبت نشده')
        self.assertContains(page, 'رفع در ابزار CNC')

    def test_registered_manual_file_flips_row_to_ready(self):
        issue = self.route_to_queue()
        register_manual_cnc_file(self.task, MINIMAL_1890, self.user)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'resolved')
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'آمادهٔ Nesting')
        self.assertContains(page, 'فایل دستی آماده')
        self.assertContains(page, 'Nesting')
        self.assertNotContains(page, 'در انتظار اصلاح دستی')

    def test_approved_profile_signal_appears_on_open_row(self):
        self.route_to_queue()
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'در انتظار اصلاح دستی')
        self.assertContains(page, 'الگوی خودکار تأییدشده آماده')

    def test_part_template_signal_appears(self):
        self.route_to_queue()
        CncPartTemplate.objects.create(
            part=self.part, name='قالب آزمون', payload=[],
            roles=[], source_bounds={}, created_by=self.user,
        )
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertContains(page, 'قالب ویرایش هندسی دستی ثبت شده')

    def test_part_template_signal_shows_names_and_backlink(self):
        """فاز ۲.۲ — سیگنال قالب، نام قالب‌ها را هم نشان می‌دهد و لینک ابزار
        با ‎?from=manual‎ برمی‌گردد؛ کلیدهای بولی قبلی معنایشان حفظ می‌شود."""
        self.route_to_queue()
        CncPartTemplate.objects.create(
            part=self.part, name='آرایهٔ پیچ گوشه', payload=[],
            roles=[], source_bounds={}, created_by=self.user,
        )
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'قالب ویرایش هندسی دستی ثبت شده')
        self.assertContains(page, 'آرایهٔ پیچ گوشه')
        self.assertContains(page, 'from=manual')
        rows = page.context['rows']
        self.assertEqual(len(rows), 1)
        signals = rows[0]['signals']
        self.assertTrue(signals['part_template_ready'])
        self.assertEqual(signals['part_template_names'], ['آرایهٔ پیچ گوشه'])
        # کلیدهای فاز ۲.۱ دست‌نخورده
        self.assertFalse(signals['manual_file_ready'])
        self.assertFalse(signals['profile_ready'])

    def test_part_template_names_empty_without_templates(self):
        """فاز ۲.۲ — بدون قالب، کلید additive خالی است و badge رندر نمی‌شود."""
        self.route_to_queue()
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'قالب ویرایش هندسی دستی ثبت شده')
        signals = page.context['rows'][0]['signals']
        self.assertFalse(signals['part_template_ready'])
        self.assertEqual(signals['part_template_names'], [])

    def test_row_disappears_once_pieces_are_placed(self):
        self.route_to_queue()
        register_manual_cnc_file(self.task, MINIMAL_1890, self.user)
        plans, _issues, _report = plan_cnc_with_report([self.order.pk], self.user)
        self.assertEqual(len(plans), 1)
        page = self.client.get(reverse('cnc_resizer:manual_queue'))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, f'#{self.task.pk}')

    def test_page_requires_operator_permission(self):
        self.route_to_queue()
        outsider = User.objects.create_user('no-cnc-page', 'n@example.test', 'pw')
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse('cnc_resizer:manual_queue')).status_code, 403)

    def test_sheet_page_links_the_queue_with_count(self):
        self.route_to_queue()
        page = self.client.get(reverse('cnc_resizer:sheet_plans'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, reverse('cnc_resizer:manual_queue'))
        self.assertContains(page, '(1)')


@override_settings(CNC_RESIZER_SHEET_ADAPTER='minimal_xy_router')
class RenestTests(SiteSettingsFixtureMixin, ManualQueueFixtureMixin, TestCase):
    def post_renest(self, issue_id, next_path='/cnc/manual/'):
        return self.client.post(reverse('cnc_resizer:manual_renest'), {
            'issue_id': issue_id, 'next': next_path,
        })

    def test_renest_after_manual_file_nests_and_reports(self):
        CncPlanningIssue.objects.create(
            task=self.task, order=self.order, part=self.part, mode='cnc_sheets',
            reason='تست', details={'stage': 'manual_required', 'reason_code': 'no_profile', 'quantity': 1},
            created_by=self.user,
        )
        register_manual_cnc_file(self.task, MINIMAL_1890, self.user)
        issue = CncPlanningIssue.objects.get(task=self.task)
        response = self.post_renest(issue.pk)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/cnc/manual/')
        page = self.client.get(response['Location'])
        self.assertContains(page, 'Nesting انجام شد')
        self.assertContains(page, 'با فایل دستی ثبت‌شده چیده شد')
        self.assertEqual(CncSheetPlan.objects.count(), 1)
        plan = CncSheetPlan.objects.get()
        self.assertTrue(plan.output_verified)

    def test_renest_with_approved_profile_resolves_open_issue(self):
        CncPlanningIssue.objects.create(
            task=self.task, order=self.order, part=self.part, mode='cnc_sheets',
            reason='تست', details={'stage': 'manual_required', 'reason_code': 'no_profile', 'quantity': 1},
            created_by=self.user,
        )
        CncPartProfile.objects.create(
            part=self.base_part,
            source_file=CncFile.objects.get(barcode_key='panel.manual.1740x400', kind='base'),
            params={'mode': 'equal', 'symmetric': True}, approved=True,
        )
        issue = CncPlanningIssue.objects.get(task=self.task, status='open')
        response = self.post_renest(issue.pk)
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response['Location'])
        self.assertContains(page, 'با الگوی تأییدشده اصلاح و چیده شد')
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'resolved')
        self.assertIn('Nesting موفق', issue.resolution_note)
        self.assertEqual(CncSheetPlan.objects.count(), 1)

    def test_blocked_renest_keeps_issue_open_fail_closed(self):
        CncPlanningIssue.objects.create(
            task=self.task, order=self.order, part=self.part, mode='cnc_sheets',
            reason='تست', details={'stage': 'manual_required', 'reason_code': 'no_profile', 'quantity': 1},
            created_by=self.user,
        )
        issue = CncPlanningIssue.objects.get(task=self.task, status='open')
        response = self.post_renest(issue.pk)
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response['Location'])
        self.assertContains(page, 'Nesting هنوز ممکن نشد')
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'open')
        self.assertEqual(CncSheetPlan.objects.count(), 0)

    def test_renest_requires_operator_permission(self):
        outsider = User.objects.create_user('no-cnc-renest', 'n@example.test', 'pw')
        self.client.force_login(outsider)
        response = self.post_renest(1)
        self.assertEqual(response.status_code, 403)

    def test_renest_rejects_non_manual_issue(self):
        issue = CncPlanningIssue.objects.create(
            task=self.task, order=self.order, part=self.part, mode='cnc_sheets',
            reason='ورق آزاد نشد', details={'stage': 'stock'}, created_by=self.user,
        )
        response = self.post_renest(issue.pk)
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response['Location'])
        self.assertContains(page, 'جزو صف دستی نیست')
        self.assertEqual(CncSheetPlan.objects.count(), 0)

    def test_renest_unknown_issue_is_404(self):
        response = self.post_renest(99999)
        self.assertEqual(response.status_code, 404)


class IssueNextRedirectTests(ManualQueueFixtureMixin, TestCase):
    def open_issue(self):
        return CncPlanningIssue.objects.create(
            task=self.task, order=self.order, part=self.part, mode='cnc_sheets',
            reason='تست', details={'stage': 'manual_required', 'reason_code': 'no_profile', 'quantity': 1},
            created_by=self.user,
        )

    def test_next_param_returns_to_manual_queue(self):
        issue = self.open_issue()
        response = self.client.post(reverse('cnc_resizer:update_planning_issue', args=[issue.pk]), {
            'status': 'resolved', 'next': '/cnc/manual/', 'note': 'بررسی شد',
        })
        self.assertRedirects(response, '/cnc/manual/', fetch_redirect_response=False)

    def test_unsafe_next_falls_back_to_sheet_plans(self):
        issue = self.open_issue()
        response = self.client.post(reverse('cnc_resizer:update_planning_issue', args=[issue.pk]), {
            'status': 'resolved', 'next': 'https://evil.example/x', 'note': '',
        })
        self.assertRedirects(
            response, reverse('cnc_resizer:sheet_plans'), fetch_redirect_response=False)

    def test_default_redirect_is_unchanged(self):
        issue = self.open_issue()
        response = self.client.post(reverse('cnc_resizer:update_planning_issue', args=[issue.pk]), {
            'status': 'resolved', 'note': '',
        })
        self.assertRedirects(
            response, reverse('cnc_resizer:sheet_plans'), fetch_redirect_response=False)
