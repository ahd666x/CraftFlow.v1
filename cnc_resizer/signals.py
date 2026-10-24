"""Phase-4 (P3): part-template edits flag the affected planned sheet plans.

When a :class:`~cnc_resizer.models.CncPartTemplate` is saved or deleted, every
``planned`` :class:`~cnc_resizer.models.CncSheetPlan` whose layout contains a
piece of that part is flagged ``template_stale`` with a small audit trail
(``template_stale_info``, last 5 events). The flag is strictly ADVISORY:
existing outputs stay valid and every hard gate (``spec_hash`` staleness,
dialect verification, ownership) is unchanged — the badge only tells the
operator «the parametric template moved after nesting; consider re-nesting».

Notes:
- ``post_save``/``post_delete`` only: queryset ``update()``-style edits do not
  fire signals (template edits go through the audited API views).
- Matching walks planned plans in Python and checks ``layout.parts`` — the
  planned-plan count is small and this stays DB-agnostic (no JSON path lookups).
- ``raw`` saves (fixtures/loaddata) are skipped.
"""
from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import CncPartTemplate, CncSheetPlan

_MAX_INFO_ENTRIES = 5


def _flag_plans_for_part(part_id, entry):
    """Flag planned plans containing this part; returns number of flagged rows."""
    if part_id is None:
        return 0
    flagged = 0
    plans = CncSheetPlan.objects.filter(status='planned').only(
        'id', 'layout', 'template_stale', 'template_stale_info')
    for plan in plans.iterator():
        parts = (plan.layout or {}).get('parts') or []
        if not any(piece.get('part_id') == part_id for piece in parts):
            continue
        info = [row for row in (plan.template_stale_info or [])
                if row.get('template_id') != entry['template_id']]
        info.append(entry)
        plan.template_stale = True
        plan.template_stale_info = info[-_MAX_INFO_ENTRIES:]
        plan.save(update_fields=['template_stale', 'template_stale_info'])
        flagged += 1
    return flagged


@receiver(post_save, sender=CncPartTemplate)
def flag_plans_on_template_save(sender, instance, **kwargs):
    if kwargs.get('raw'):
        return
    _flag_plans_for_part(instance.part_id, {
        'template_id': instance.pk,
        'name': instance.name,
        'part_id': instance.part_id,
        'event': 'saved',
        'at': timezone.now().isoformat(),
    })


@receiver(post_delete, sender=CncPartTemplate)
def flag_plans_on_template_delete(sender, instance, **kwargs):
    if kwargs.get('raw'):
        return
    _flag_plans_for_part(instance.part_id, {
        'template_id': instance.pk,
        'name': instance.name,
        'part_id': instance.part_id,
        'event': 'deleted',
        'at': timezone.now().isoformat(),
    })
