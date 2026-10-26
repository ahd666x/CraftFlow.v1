"""Read-side model of the CNC manual queue (phase 2.1).

The manual queue is the operator-facing list of production tasks whose CNC
file could not be produced automatically and were routed here by the planner
(``CncPlanningIssue`` with ``details.stage == 'manual_required'``). This module
answers one question per task: *what is the current blocker state, and is
re-nesting worth pressing now?*

Honesty contract — everything here is READ-ONLY:
- No file is registered, derived or approved by rendering the queue. Signals
  only REPORT state that other, already-audited flows created:
    ``manual_file_ready``     — an operator-registered manual fix exists on
                                disk for exactly (part, effective length),
                                i.e. ``resolve_cnc_file`` will pick it up.
    ``profile_ready``         — an APPROVED resize profile exists, so the
                                planner's auto-derive path can succeed.
    ``part_template_ready``   — a parametric part template exists, so the
                                operator can produce the fixed file quickly
                                in the CAD tool (it never bypasses checks).
    ``part_template_names``   — phase 2.2, additive: the NAMES of those
                                templates, so the queue badge points the
                                operator to the right one in the CAD tool.
- Rows with ``task = None`` (order/task deleted → SET_NULL) are skipped: they
  have no deep link, no effective spec and no pending quantity to show. They
  remain visible in the «نیازمند بررسی» drawer of the sheet page.
- Resolved issues stay listed only while the task still has UNPLACED pending
  pieces (resolved within the last 30 days) — that is exactly the «ready for
  re-nest» state the Re-nest action consumes. Once the planner places the
  pieces (or the task completes), the row disappears on its own; nothing is
  hidden manually.
"""
import os
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import CncFile, CncPartProfile, CncPartTemplate, CncPlanningIssue
from .services import canonical_barcode_key, manual_derived_key
from .sheet_nesting import _planned_piece_counts  # the planner's own count — never drifts

# Resolved manual_required issues remain visible for re-nest for this long.
RESOLVED_VISIBLE_DAYS = 30

REASON_LABELS = {
    'no_profile': 'بدون الگوی تأییدشده',
    'profile_failed': 'شکست تولید خودکار',
    'width_changed': 'تغییر عرض',
    'thickness_changed': 'تغییر ضخامت',
}

STATE_LABELS = {
    'waiting': 'در انتظار اصلاح دستی',
    'ready': 'آمادهٔ Nesting',
}


def _may_view_all(user):
    from cutting.permissions import can_manage
    return bool(user.is_staff or user.is_superuser or can_manage(user))


def _issue_query(user):
    # Open blockers of any age + recently resolved rows (the re-nest state).
    query = CncPlanningIssue.objects.filter(
        Q(status='open')
        | Q(status='resolved', resolved_at__gte=resolved_cutoff()),
        mode='cnc_sheets', details__stage='manual_required',
    ).select_related('task', 'order', 'part', 'task__part', 'task__part__material', 'resolved_by')
    if not _may_view_all(user):
        query = query.filter(created_by=user)
    return query


def _pending_specs(tasks):
    """بستهٔ specs برای همهٔ تسک‌ها در یک فراخوانی (۳ کوئری برای کل صف، نه برای هر ردیف).

    بازبینی دور ۳ (عملکرد): فراخوانی per-row با effective_specs([task]) روی صف
    بزرگ ~۷–۹ کوئری به‌ازای هر ردیف می‌زد و layout برنامه‌های برش را برای هر
    ردیف از نو می‌خواند. اگر کل بسته به‌خاطر یک تسک خراب پرتاب شد، فقط همان
    تسک حذف می‌شود (fail-soft قبلی حفظ شده)."""
    from cutting.specs import effective_specs
    try:
        return {spec.task_id: spec for spec in effective_specs(tasks)}
    except Exception:
        by_task = {}
        for task in tasks:
            try:
                by_task[task.pk] = effective_specs([task])[0]
            except Exception:
                continue
        return by_task


def _base_record_readonly(part):
    """Newest indexed base CncFile for the part's barcode — filter ONLY.

    Unlike ``services._base_record_for_part`` this never registers a missing
    archive file: rendering the queue must not mutate the archive.
    """
    base = part.base_part or part
    return CncFile.objects.filter(
        barcode_key=canonical_barcode_key(base.f3), kind=CncFile.KIND_BASE,
    ).order_by('-created_at', '-id').first()


def _manual_file_signal(part, spec_length):
    """True when an operator-registered manual fix exists for exactly this length."""
    record = _base_record_readonly(part)
    if record is None:
        return False
    key = manual_derived_key(record, part.pk, spec_length)
    manual = CncFile.objects.filter(
        barcode_key=key, kind=CncFile.KIND_DERIVED,
    ).first()
    if manual is None or (manual.params or {}).get('mode') != 'manual':
        return False
    return bool(manual.path and os.path.isfile(manual.path))


def _profile_signal(part):
    """True when an approved, usable resize profile covers this part."""
    for candidate in (part, part.base_part):
        if candidate is None:
            continue
        try:
            profile = candidate.cnc_profile
        except CncPartProfile.DoesNotExist:
            continue
        if profile.approved and profile.source_file_id and profile.source_file.resizable:
            return True
    return False


def _signals_for(part, spec_length):
    """Advisory read-only signals for one row.

    ``part_template_names`` (phase 2.2) is an additive key — the docstring
    above is the honesty contract; the booleans keep their exact meaning.
    """
    templates = list(
        CncPartTemplate.objects.filter(part=part).order_by('name')
        .values('id', 'name')
    )
    return {
        'manual_file_ready': _manual_file_signal(part, spec_length),
        'profile_ready': _profile_signal(part),
        'part_template_ready': bool(templates),
        'part_template_names': [template['name'] for template in templates],
    }


def queue_rows(user):
    """Rows for the manual queue page, one per production task.

    Each row: ``issue`` (representative), ``state`` (waiting|ready),
    ``pending`` pieces still to place, ``signals`` and display fields.
    Raises nothing per-row: a task deleted after planning (SET_NULL) or an
    unreadable spec only drops that row's signals, never the page.
    """
    issues = list(_issue_query(user).order_by('task_id', '-created_at', '-id'))
    by_task = {}
    for issue in issues:
        if issue.task_id is None:
            continue  # orphaned rows stay in the sheet-page drawer
        rows = by_task.setdefault(issue.task_id, [])
        rows.append(issue)

    already_planned = _planned_piece_counts()
    # بازبینی دور ۳: specs همهٔ تسک‌ها یک‌جا حل می‌شود (حذف N+1 هر ردیف)
    specs_by_task = _pending_specs(
        [task_issues[0].task for task_issues in by_task.values()
         if task_issues[0].task is not None and task_issues[0].task.part_id])
    rows_out = []
    for task_id, task_issues in by_task.items():
        task = task_issues[0].task
        if task is None or task.part is None:
            continue
        open_issue = next((item for item in task_issues if item.status == 'open'), None)
        representative = open_issue or task_issues[0]
        spec = specs_by_task.get(task.pk)
        pending = None
        if spec is not None:
            try:
                pending = max(0, int(spec.quantity_total) - int(task.completed_quantity)
                              - already_planned.get(task.pk, 0))
            except Exception:
                pending = None
        if not pending:
            continue  # nothing left to place → re-nest is meaningless
        part = task.part
        signals = {'manual_file_ready': False, 'profile_ready': False, 'part_template_ready': False}
        if spec is not None:
            try:
                signals = _signals_for(part, spec.length)
            except Exception:
                pass  # fail-soft: signals are advisory, never blocking
        rows_out.append({
            'issue': representative,
            'task': task,
            'task_id': task.pk,
            'order_id': task.order_id,
            'part': part,
            'order': task.order,
            'pending': pending,
            'state': 'waiting' if open_issue is not None else 'ready',
            'reason_code': (representative.details or {}).get('reason_code', ''),
            'reason_label': REASON_LABELS.get(
                (representative.details or {}).get('reason_code', ''), 'بررسی'),
            'reason_text': representative.reason,
            'signals': signals,
            'can_renest': open_issue is not None or signals['manual_file_ready'] or signals['profile_ready'],
            'resolution_note': representative.resolution_note,
        })
    # Waiting rows first, newest first inside each state group.
    rows_out.sort(key=lambda row: (
        0 if row['state'] == 'waiting' else 1,
        -row['issue'].created_at.timestamp(),
    ))
    return rows_out


def resolved_cutoff():
    return timezone.now() - timedelta(days=RESOLVED_VISIBLE_DAYS)
