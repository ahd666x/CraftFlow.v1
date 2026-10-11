# -*- coding: utf-8 -*-
with open(r'C:\Users\Hossein Nezhad\Desktop\CraftFlow V1\CraftFlow\product\views.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = "return JsonResponse({'success': False, 'error': result.get('error', 'تسک‌ها قبلاً ایجاد شده‌اند.')})}\n\n\n\n\n@login_required\n@admin_or_manager_required\ndef dashboard(request):"

new = "return JsonResponse({'success': False, 'error': result.get('error', 'تسک‌ها قبلاً ایجاد شده‌اند.')})}\n\n\ndef root_view(request):\n    \"\"\"\n    ریشه سایت:\n    - اگر کاربر احراز هویت شده و ادمین/مدیر (گروه ۲ یا ۳ یا سوپریوزر) است → داشبورد مدیریت\n    - در غیر این صورت → هدایت به فروشگاه (/shop/)\n    \"\"\"\n    if request.user.is_authenticated:\n        if request.user.is_superuser:\n            return dashboard(request)\n        if request.user.groups.filter(name__in=['2', '3']).exists():\n            return dashboard(request)\n    return redirect('storefront:home')\n\n\n@login_required\n@admin_or_manager_required\ndef dashboard(request):"

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\Hossein Nezhad\Desktop\CraftFlow V1\CraftFlow\product\views.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Success!')
else:
    print('Old text not found')
    idx = content.find("return JsonResponse({'success': False")
    print("Found at:", idx)