# product/templatetags/selvi_tags.py
from django import template
from django.utils.safestring import mark_safe

register = template.Library()

# ── Status badge mapping ──────────────────────────────────────────────
_STATUS_CSS = {
    'draft':      'badge-status draft',
    'planned':    'badge-status planned',
    'producing':  'badge-status producing',
    'completed':  'badge-status completed',
    'shipped':    'badge-status shipped',
    'packing':    'badge-status packing',
    'in-transit': 'badge-status in-transit',
    'ready':      'badge-status ready',
    'pending':    'badge-status pending',
    'waiting':    'badge-status waiting',
    'done':       'badge-status done',
}

# Persian labels for statuses
_STATUS_LABELS = {
    'draft':      'پیش‌نویس',
    'planned':    'برنامه‌ریزی شده',
    'producing':  'در حال تولید',
    'completed':  'تکمیل شده',
    'shipped':    'ارسال شده',
    'packing':    'بسته‌بندی',
    'in-transit': 'در حمل و نقل',
    'ready':      'آماده',
    'pending':    'در انتظار',
    'waiting':    'صبر',
    'done':       'اتمام یافته',
}


@register.filter(name='status_badge')
def status_badge(status, label=None):
    """Render a standardized status badge span. Pass label=None to use Persian default."""
    css_class = _STATUS_CSS.get(status, 'badge-status bg-secondary')
    display = label if label else _STATUS_LABELS.get(status, status)
    return mark_safe(
        f'<span class="{css_class}">{display}</span>'
    )


@register.filter(name='status_color_class')
def status_color_class(status):
    """Return just the CSS class string for a given status (for use in templates)."""
    return _STATUS_CSS.get(status, 'badge-status bg-secondary')


# ── Pagination inclusion tag ──────────────────────────────────────────

@register.inclusion_tag('painting_management/_pagination.html', takes_context=True)
def paginate(context, page_obj, param_name='page'):
    """
    Render pagination controls.

    Usage in template:
        {% load selvi_tags %}
        {% paginate workers as page_obj %}

    Or with a custom variable name:
        {% paginate workers %}

    The page_obj can be a Paginator Page object or a string referencing
    a context variable holding one.
    """
    request = context['request']
    query = request.GET.copy()
    return {
        'page_obj': page_obj,
        'param_name': param_name,
        'request': request,
    }
