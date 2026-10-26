from .permissions import can_operate, can_inventory, can_manage


SECTION_BY_URL = {
    'planner': 'cut', 'planner_preview': 'cut', 'plans': 'cut', 'plan_detail': 'cut',
    'download_cut': 'cut', 'mark_cut': 'cut', 'cancel_plan': 'cut',
    'inventory': 'stock', 'full_sheets': 'stock', 'offcuts': 'stock',
    'stock_create': 'stock', 'stock_edit': 'stock',
    'history': 'history',
    'size_specs': 'settings', 'size_specs_edit': 'settings', 'policy': 'settings',
}


def cutting_nav(request):
    match = getattr(request, 'resolver_match', None)
    if not match or match.app_name not in ('cutting', 'cnc_resizer'):
        return {}
    user = request.user
    if match.app_name == 'cnc_resizer':
        # بازبینی UI/UX بند ۱: صفحه‌های CNC تب خودشان را فعال کنند، نه «برش».
        section = 'cnc-manual' if match.url_name == 'manual_queue' else 'cnc'
        return {
            'cut_section': section,
            'cut_url_name': match.url_name,
            'can_operate': True,
            'can_inventory': can_inventory(user),
            'can_manage': can_manage(user),
        }
    return {
        'cut_section': SECTION_BY_URL.get(match.url_name, ''),
        'cut_url_name': match.url_name,
        'can_operate': can_operate(user),
        'can_inventory': can_inventory(user),
        'can_manage': can_manage(user),
    }
