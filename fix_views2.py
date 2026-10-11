# -*- coding: utf-8 -*-
with open(r'C:\Users\Hossein Nezhad\Desktop\CraftFlow V1\CraftFlow\product\views.py', 'rb') as f:
    content = f.read()

old = b"return JsonResponse({'success': False, 'error': result.get('error', '\xd8\xaa\xd8\xb3\xda\xa9\xe2\x80\x8c\xd9\x87\xd8\xa7 \xd9\x82\xd8\xa8\xd9\x84\xd8\xa7\xd9\x8b \xd8\xa7\xdb\x8c\xd8\xac\xd8\xa7\xd8\xaf \xd8\xb4\xd8\xaf\xd9\x87\xe2\x80\x8c\xd8\xa7\xd9\x86\xd8\xaf.')})\n\n\n\n\n@login_required\n@admin_or_manager_required\ndef dashboard(request):"

new = b"return JsonResponse({'success': False, 'error': result.get('error', '\xd8\xaa\xd8\xb3\xda\xa9\xe2\x80\x8c\xd9\x87\xd8\xa7 \xd9\x82\xd8\xa8\xd9\x84\xd8\xa7\xd9\x8b \xd8\xa7\xdb\x8c\xd8\xac\xd8\xa7\xd8\xaf \xd8\xb4\xd8\xaf\xd9\x87\xe2\x80\x8c\xd8\xa7\xd9\x86\xd8\xaf.')})\n\n\ndef root_view(request):\n    \"\"\"\n    Root view: redirect to dashboard for admin/manager, shop for others.\n    \"\"\"\n    if request.user.is_authenticated:\n        if request.user.is_superuser:\n            return dashboard(request)\n        if request.user.groups.filter(name__in=['2', '3']).exists():\n            return dashboard(request)\n    from django.shortcuts import redirect\n    return redirect('storefront:home')\n\n\n@login_required\n@admin_or_manager_required\ndef dashboard(request):"

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\Hossein Nezhad\Desktop\CraftFlow V1\CraftFlow\product\views.py', 'wb') as f:
        f.write(content)
    print('Success!')
else:
    print('Old text not found')