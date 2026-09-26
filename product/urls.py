# urls.py
from django.urls import path
from . import views
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from .views import painting_holidays_view  


urlpatterns = [
    path('painting/holidays/', views.painting_holidays_view, name='painting_holidays'),

    path('painting/api/workers/', views.painting_workers_api, name='painting_workers_api'),
    path('painting/api/workers/<int:worker_id>/', views.painting_worker_detail_api, name='painting_worker_detail_api'),

    # دو مسیر مجزا با نام‌های متفاوت
    path('painting/api/workers/<int:worker_id>/excluded-products/',
         views.painting_worker_exclusion_api,
         name='painting_worker_excluded_products_api'),   # ← نام جدید

    path('painting/api/workers/<int:worker_id>/excluded-items/',
         views.painting_worker_exclusion_api,
         name='painting_worker_excluded_items_api'),      # ← نام جدید

    path('painting/api/products/search/', views.search_products_api, name='search_products_api'),
    path('painting/api/items/search/', views.search_items_api, name='search_items_api'),

    # مدیریت سفارشات (ادمین)
    path('management/orders/<int:order_id>/edit/', views.admin_edit_order, name='admin_edit_order'),
    path('management/orders/item/<int:item_id>/delete/', views.admin_delete_order_item, name='admin_delete_order_item'),
    path('management/orders/<int:order_id>/add-item/', views.admin_add_order_item, name='admin_add_order_item'),
    path('management/orders/<int:order_id>/delete/', views.admin_delete_order, name='admin_delete_order'),
    path('management/orders/item/<int:item_id>/edit/', views.admin_edit_order_item, name='admin_edit_order_item'),
    # (اختیاری) ویرایش آیتم توسط ادمین – می‌توانید از customer_edit_order_item با تغییر دسترسی استفاده کنید
    # اما بهتر است یک ویو مجزا بسازیم
    # path('admin/orders/item/<int:item_id>/edit/', views.admin_edit_order_item, name='admin_edit_order_item'),
    # مدیریت تسک‌های سفارش
    path('management/orders/<int:order_id>/tasks/', views.admin_order_tasks, name='admin_order_tasks'),
    path('management/task/<int:task_id>/delete/', views.admin_delete_task, name='admin_delete_task'),
    path('management/tasks/', views.admin_tasks_management, name='admin_tasks_management'),
    path('archive/upload/', views.archive_upload, name='archive_upload'),
    path('archive/upload/cnc/', views.archive_upload_cnc, name='archive_upload_cnc'),
    path('archive/upload/dr/', views.archive_upload_dr, name='archive_upload_dr'),
    path('archive/delete-file/', views.archive_delete_file, name='archive_delete_file'),
    path('archive/download/<str:archive_type>/<str:filename>/', views.archive_download, name='archive_download'),
    path('', views.dashboard, name='dashboard'),
    path('orders/', views.order_list, name='order_list'),
    path('orders/<int:order_id>/items-expand/', views.order_items_expand_ajax, name='order_items_expand_ajax'),
    path('orderlist/', views.order_item_list, name='order_item_list'),

    # چاپ
    path('orders/<int:order_id>/print/', views.order_print, name='order_print'),
    path('item/<int:pk>/print/', views.print_sheet, name='print_sheet'),
    path('item/<int:pk>/print_lable/', views.print_lable, name='print_lable'),

    path('item/<int:pk>/', views.item_detail, name='item_detail'),
    path('scan/<int:pk>/', views.scan_qr, name='scan_qr'),

    path('reports/shipped/', views.report_shipped, name='report_shipped'),
    path('reports/ready-to-ship/', views.report_ready_to_ship, name='report_ready_to_ship'),
    path('reports/fulfillment-status/', views.report_fulfillment_status, name='report_fulfillment_status'),
    # path('reports/shipped/print/', views.delivery_note_print, name='delivery_note_print'),

    
    # گزارش‌ها
    path('reports/orders/', views.report_orders, name='report_orders'),
    path('reports/stages/', views.report_stages, name='report_stages'),
    path('reports/production-unified/', views.report_production_unified, name='report_production_unified'),
    path('reports/workers/', views.report_workers, name='report_workers'),
    path('reports/delayed/', views.delayed_orders, name='report_delayed'),
    path('reports/material-consumption/', views.report_material_consumption, name='report_material_consumption'),
    path('production/defects/', views.production_defects, name='product_defects'),
    path('ajax/defects/order/<int:order_id>/units/', views.ajax_order_units_for_defect, name='ajax_order_units_for_defect'),
    path('ajax/defects/unit/<int:unit_id>/color-parts/', views.ajax_unit_color_parts_for_defect, name='ajax_unit_color_parts_for_defect'),
    path('ajax/defects/order/<int:order_id>/items/', views.ajax_order_items_for_defect, name='ajax_order_items_for_defect'),
    path('ajax/defects/item/<int:item_id>/color-parts/', views.ajax_item_color_parts_for_defect, name='ajax_item_color_parts_for_defect'),

    path('upload/', views.upload_form, name='upload_form'),

    path('scan/part/', views.scan_part, name='scan_part'),


    # path('lable/part/', views.lable_part, name='lable_part'),
    # path('lable/part/<int:print_lable_part>/', views.print_lable_part, name='print_lable_part'),

    # path('scan/part/<str:barcode>/', views.scan_part_direct, name='scan_part_direct'),


    path('orders/create/', views.create_order, name='create_order'),
    path('orders/create/step2/<int:order_id>/', views.create_order_step2, name='create_order_step2'),
    path('ajax/load-products/', views.ajax_load_products, name='ajax_load_products'),
    path('ajax/load-customers/', views.ajax_load_customers, name='ajax_load_customers'),

    path('orders/<int:order_id>/', views.order_detail, name='order_detail'),

    path('orders/<int:order_id>/export-autocut/', views.export_autocut_xml, name='export_autocut_xml'),
    path('orders/export-multiple-autocut/', views.export_multiple_autocut, name='export_multiple_autocut'),

    path('scan/part/cnc/', views.scan_part_cnc, name='scan_part_cnc'),
    path('cnc/download/<str:barcode>/', views.download_cnc_file, name='download_cnc_file'),
    path('scan/part/dr/', views.scan_part_dr, name='scan_part_dr'),
    path('dr/download/<str:barcode>/', views.download_dr_file, name='download_dr_file'),
    path('tasks/<int:task_id>/mark-done/', views.mark_task_done, name='mark_task_done'),
    path('scan/item-tasks/<int:item_id>/', views.scan_item_tasks_ajax, name='scan_item_tasks_ajax'),

    # فروشگاه مشتریان
    # path('shop/', views.shop_product_list, name='shop_product_list'),
    # path('shop/product/<int:pk>/', views.shop_product_detail, name='shop_product_detail'),
    # path('shop/cart/add/', views.cart_add, name='cart_add'),
    # path('shop/cart/', views.cart_view, name='cart_view'),
    # path('shop/cart/update/', views.cart_update, name='cart_update'),
    # path('shop/checkout/', views.checkout, name='checkout'),
    # path('shop/order/<int:order_id>/', views.shop_order_tracking, name='shop_order_tracking'),
    # path('ajax/load-customers/', views.ajax_load_customers, name='ajax_load_customers'),
    # path('shop/orders/', views.shop_order_history, name='shop_order_history'),
    # path('shop/order/<int:order_id>/', views.shop_order_tracking, name='shop_order_tracking'),


     path('products/', views.admin_product_list, name='admin_product_list'),
     path('products/<int:product_id>/toggle-active/', views.product_toggle_active, name='product_toggle_active'),
     path('catalog/', views.product_catalog, name='product_catalog'),
     path('catalog/pdf/', views.product_catalog_pdf, name='product_catalog_pdf'),
path('product/<int:product_id>/bom/edit/', views.product_bom_edit, name='product_bom_edit'),
     path('scan/packaging/<int:pk>/', views.scan_packaging_unit, name='scan_packaging_unit'),
    path('orders/<int:order_id>/invoice/', views.order_invoice, name='order_invoice'),

    path('scan/packaging/<int:pk>/undo/', views.undo_packaging_unit, name='undo_packaging_unit'),




    # بخش مشتریان
    path('customer/orders/', views.customer_order_list, name='customer_order_list'),
    path('customer/orders/new/', views.customer_create_order, name='customer_create_order'),

    path('customer/orders/detail/<int:order_id>/', views.customer_order_detail, name='customer_order_detail'),
    path('customer/orders/item/<int:item_id>/edit/', views.customer_edit_order_item, name='customer_edit_order_item'), 
    path('customer/orders/item/<int:item_id>/delete/', views.customer_delete_order_item, name='customer_delete_order_item'),


path('orders/<int:order_id>/combined-print/', views.order_combined_print, name='order_combined_print'),


    path('customer/orders/edit-info/<int:order_id>/', views.customer_edit_order_info, name='customer_edit_order_info'),
    path('customer/orders/add-item/<int:order_id>/', views.customer_add_item, name='customer_add_item'),

    path('ajax/load-product-colors/<int:product_id>/', views.ajax_load_product_colors, name='ajax_load_product_colors'),
    path('products/create/', views.product_create, name='product_create'),
    path('products/<int:product_id>/edit/', views.product_edit, name='product_edit'),
    path('ajax/create-part/', views.ajax_create_part, name='ajax_create_part'),
    path('ajax/get-part/<int:part_id>/', views.ajax_get_part, name='ajax_get_part'),
    path('ajax/edit-part/<int:part_id>/', views.ajax_edit_part, name='ajax_edit_part'),


    path('orders/<int:order_id>/generate-tasks/', views.order_generate_tasks, name='order_generate_tasks'),
    path('shipping/set-plate/', views.set_plate, name='set_plate'),


    path('item/<int:item_id>/assign-painting/', views.assign_painting_process, name='assign_painting_process'),
    path('schedule/daily-print/', views.daily_schedule_print, name='daily_schedule_print'),
    path('schedule/auto-assign/', views.auto_assign_tasks_view, name='auto_assign_tasks'),

    # پنل مدیریت نقاشی
    path('painting/dashboard/', views.painting_management_dashboard, name='painting_dashboard'),
    path('painting/processes/', views.painting_processes_view, name='painting_processes'),
    path('painting/processes/<int:process_id>/get/', views.painting_process_detail_api, name='painting_process_detail'),
    path('painting/stages/', views.painting_stages_view, name='painting_stages'),
    path('painting/stages/<int:process_id>/', views.painting_stages_view, name='painting_stages_process'),
    path('painting/stages/<int:stage_id>/get/', views.painting_stage_detail_api, name='painting_stage_detail'),
    path('painting/api/processes/<int:process_id>/materials/', views.painting_process_materials_api, name='painting_process_materials_api'),
    path('painting/api/process-materials/<int:process_material_id>/color-variants/', views.painting_process_material_variants_api, name='painting_process_material_variants_api'),
    path('painting/api/product-colorpart-materials/', views.product_color_part_materials_api, name='product_color_part_materials_api'),
    path('painting/api/raw-materials/search/', views.search_raw_materials_api, name='painting_raw_materials_search'),
    path('painting/api/color-codes/', views.ajax_color_codes, name='ajax_color_codes'),
    path('painting/workers/', views.painting_workers_view, name='painting_workers'),
    path('painting/workers/<int:worker_id>/excluded-items/', views.painting_worker_excluded_items, name='painting_worker_excluded_items'),
    path('painting/schedule/', views.painting_schedule_view, name='painting_schedule'),
    path('painting/ready-list/', views.painting_ready_list, name='painting_ready_list'),
    path('painting/add-to-schedule/', views.painting_add_to_schedule, name='painting_add_to_schedule'),
    path('painting/create-custom-task/', views.painting_create_custom_task, name='painting_create_custom_task'),
    path('painting/assign-process/', views.painting_assign_process, name='painting_assign_process'),
    path('painting/auto-assign/', views.painting_auto_assign, name='painting_auto_assign'),
    path('painting/available-workers/', views.painting_get_available_workers, name='painting_available_workers'),
    path('painting/assign-worker/', views.painting_assign_worker, name='painting_assign_worker'),
    path('painting/unassign-worker/', views.painting_unassign_worker, name='painting_unassign_worker'),
    path('painting/delete-tasks/', views.painting_delete_tasks, name='painting_delete_tasks'),
    path('painting/clear-schedule/', views.painting_clear_schedule, name='painting_clear_schedule'),
    path('painting/reset-schedule/', views.painting_reset_schedule, name='painting_reset_schedule'),
    path('painting/repaint-items/', views.painting_repaint_items, name='painting_repaint_items'),
    path('painting/assignment-rules/', views.painting_assignment_rules_view, name='painting_assignment_rules'),
    path('orders/<int:order_id>/delete-tasks/', views.delete_all_tasks, name='delete_all_tasks'),
    path('item/<int:item_id>/delete-paint-tasks/', views.delete_paint_tasks, name='delete_paint_tasks'),
    path('orders/<int:order_id>/delete-paint-tasks/', views.delete_all_paint_tasks_for_order, name='delete_all_paint_tasks_for_order'),


    path('customer/shipments/', views.customer_shipments, name='customer_shipments'),
    path('customer/shipments/<path:plate>/<str:date>/', views.customer_shipment_detail, name='customer_shipment_detail'),

    path('delivery/', views.delivery_list, name='delivery_list'),
    path('delivery/confirm/<int:item_id>/', views.delivery_confirm, name='delivery_confirm'),

]

