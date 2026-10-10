"""
نشانی‌های پنل انبار.

فقط سه مسیر کاری واقعی وجود دارد و بقیه فقط‌خواندنی‌اند:

    /                         ← صفحهٔ اصلی: صف مواد روزانه
    /raw-material/receive/    ← اسکن و دریافت کالا
    /daily-closing/           ← کنترل و بستن روز

گزارش‌ها همه در یک مسیر‌اند و فقط با پارامتر ``tab`` از هم جدا می‌شوند.
"""
from django.urls import path

from . import views

app_name = 'inventory'

urlpatterns = [
    # ---- ۱) کار روزانهٔ انبار ----
    path('', views.daily_material_queue, name='daily_material_queue'),
    path('daily-closing/', views.daily_closing, name='daily_closing'),
    path('daily-closing/confirm/', views.daily_closing_confirm,
         name='daily_closing_confirm'),

    # ---- ۲) دریافت کالا ----
    path('raw-material/receive/scan/', views.raw_material_receive_scan,
         name='raw_material_receive_scan'),

    # ---- ۳) صف روزانه: اقدام‌ها ----
    path('daily-queue/<int:queue_id>/delivery/',
         views.daily_queue_delivery, name='daily_queue_delivery'),
    path('daily-queue/<int:queue_id>/return/',
         views.daily_queue_return, name='daily_queue_return'),
    path('daily-queue/<int:queue_id>/auto-return/',
         views.daily_queue_auto_return, name='daily_queue_auto_return'),
    path('daily-queue/batch-auto-return/',
         views.daily_queue_batch_auto_return, name='daily_queue_batch_auto_return'),
    path('daily-queue/<int:queue_id>/cancel/',
         views.daily_queue_cancel, name='daily_queue_cancel'),
    path('daily-queue/refresh/',
         views.daily_queue_refresh, name='daily_queue_refresh'),
    path('daily-queue/repair/',
         views.daily_queue_repair, name='daily_queue_repair'),
    path('daily-queue/<int:queue_id>/sources/',
         views.daily_queue_sources, name='daily_queue_sources'),
    path('daily-queue/<int:queue_id>/preview-delivery/',
         views.daily_queue_preview_delivery,
         name='daily_queue_preview_delivery'),

    # ---- تنظیمات: مواد اولیه ----
    path('materials/', views.material_list, name='material_list'),
    path('materials/<int:material_id>/detail/',
         views.material_detail_api, name='material_detail'),
    path('materials/create/', views.material_create, name='material_create'),
    path('materials/<int:material_id>/edit/',
         views.material_edit, name='material_edit'),
    path('materials/<int:material_id>/delete/',
         views.material_delete, name='material_delete'),

    # ---- تنظیمات: دسته‌ها ----
    path('categories/', views.category_list, name='category_list'),
    path('categories/<int:category_id>/detail/',
         views.category_detail_api, name='category_detail'),
    path('categories/create/', views.category_create, name='category_create'),
    path('categories/<int:category_id>/edit/',
         views.category_edit, name='category_edit'),
    path('categories/<int:category_id>/delete/',
         views.category_delete, name='category_delete'),

    # ---- گزارش‌ها ----
    path('reports/', views.reports_view, name='reports'),
    path('reports/ledger/export/', views.material_ledger_export,
         name='material_ledger_export'),
    path('audit/data-integrity/', views.data_integrity_audit,
         name='data_integrity_audit'),
    path('orders/<int:order_id>/material-trace/',
         views.order_material_traceability, name='order_material_traceability'),

    # ---- شمارش انبار ----
    path('stock-counts/', views.stock_count_list, name='stock_count_list'),
    path('stock-counts/create/', views.stock_count_create, name='stock_count_create'),
    path('stock-counts/<int:count_id>/', views.stock_count_detail, name='stock_count_detail'),
    path('stock-counts/<int:count_id>/approve/', views.stock_count_approve, name='stock_count_approve'),
    path('stock-counts/reverse-purchase/', views.stock_count_reverse_purchase, name='stock_count_reverse_purchase'),

    # ---- اصلاح موجودی ----
    path('stock/adjust/', views.stock_adjustment, name='stock_adjustment'),
]