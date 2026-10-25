"""Unified Selvi output-file naming for CUT/CNC programs.

One shared logic for every download path (direct download, re-download,
JSON filename payloads) so the same program always produces the same
filename. Pattern:

    Selvi_Order-{order_number}_Program-{program_number}_{type}.{extension}

- Order number(s) come from real ``Order.number`` data (fallback: order pk
  when the number is blank). Plans covering several orders join the real
  numbers with ``-``.
- Program number is real data, never random/constant: for a single-order
  plan it is the plan's 1-based sequence among that order's programs of the
  same kind (stable: ordered by ``created_at`` then ``pk``, plans are
  cancelled — not deleted); otherwise the plan pk.
- Only the download name changes; stored/internal file names and the real
  file content/format stay untouched. Windows-invalid characters are
  replaced with ``-``.
"""


def sanitize_token(value):
    """Filename-safe token: collapse whitespace and replace invalid chars."""
    import re
    cleaned = re.sub(r'\s+', '', str(value if value is not None else ''))
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '-', cleaned)
    return cleaned or 'NA'


def order_token(order_numbers):
    """'386' for one order, '386-387' for several; 'NA' when unknown."""
    tokens = []
    for value in order_numbers:
        token = sanitize_token(value)
        if token and token not in tokens:
            tokens.append(token)
    return '-'.join(tokens) if tokens else 'NA'


def program_token(number):
    """Zero-padded program number (2+ digits) from a real integer id/sequence."""
    try:
        return f'{int(number):02d}'
    except (TypeError, ValueError):
        return sanitize_token(number)


def output_filename(order_numbers, program_number, kind, extension):
    """Selvi_Order-386_Program-01_CUT.CUT / Selvi_Order-386_Program-02_CNC.nc"""
    return (
        f'Selvi_Order-{order_token(order_numbers)}'
        f'_Program-{program_token(program_number)}'
        f'_{sanitize_token(kind)}.{sanitize_token(extension)}'
    )


def order_display_number(order):
    """Real order number for filenames; falls back to the order pk."""
    return (getattr(order, 'number', '') or '').strip() or str(order.pk)


def plan_order_numbers(plan):
    """Real order numbers of a CutPlan (M2M ``orders``)."""
    return [order_display_number(order) for order in plan.orders.all()]


def cut_program_number(plan):
    """Program number of a CutPlan in its own context (see module docstring)."""
    order_ids = list(plan.orders.values_list('pk', flat=True))
    if len(order_ids) != 1:
        return plan.pk
    from .models import CutPlan
    siblings = (
        CutPlan.objects.filter(orders__pk=order_ids[0])
        .order_by('created_at', 'pk')
        .values_list('pk', flat=True)
    )
    for position, pk in enumerate(siblings, 1):
        if pk == plan.pk:
            return position
    return plan.pk


def sheet_plan_order_numbers(plan):
    """Real order numbers covered by a CncSheetPlan (via its layout pieces)."""
    from product.models import Order
    order_ids = sorted({
        int(piece['order_id'])
        for piece in (plan.layout or {}).get('parts', [])
        if piece.get('order_id') is not None
    })
    if not order_ids:
        return []
    numbers = dict(
        Order.objects.filter(pk__in=order_ids)
        .values_list('pk', 'number')
    )
    return [
        (numbers.get(order_id) or '').strip() or str(order_id)
        for order_id in order_ids
    ]


def layout_order_ids(plan):
    """Distinct order ids covered by a CncSheetPlan layout."""
    return {
        int(piece['order_id'])
        for piece in (plan.layout or {}).get('parts', [])
        if piece.get('order_id') is not None
    }


def cnc_program_number(plan):
    """Program number of a CncSheetPlan in its own context (module docstring).

    The layout JSON cannot be queried order-wise portably, so siblings are
    walked in the same stable order (created_at, pk) limited to rows created
    no later than this plan — workshop-scale tables keep this cheap.
    """
    wanted = layout_order_ids(plan)
    if len(wanted) != 1:
        return plan.pk
    from cnc_resizer.models import CncSheetPlan
    siblings = (
        CncSheetPlan.objects.filter(created_at__lte=plan.created_at)
        .order_by('created_at', 'pk')
        .values_list('pk', 'layout')
    )
    position = 0
    for pk, layout in siblings:
        if pk == plan.pk:
            return position + 1
        if wanted & layout_order_ids_from_layout(layout):
            position += 1
    return plan.pk


def layout_order_ids_from_layout(layout):
    """Same as :func:`layout_order_ids` for a raw layout dict."""
    return {
        int(piece['order_id'])
        for piece in (layout or {}).get('parts', [])
        if piece.get('order_id') is not None
    }
