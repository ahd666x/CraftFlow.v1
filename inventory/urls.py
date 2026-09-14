from django.urls import path
from . import views

app_name = 'inventory'

urlpatterns = [
    path('', views.inventory_dashboard, name='dashboard'),
    path('suppliers/', views.supplier_list, name='supplier_list'),
    path('suppliers/<int:supplier_id>/detail/', views.supplier_detail_api, name='supplier_detail'),
    path('suppliers/create/', views.supplier_create, name='supplier_create'),
    path('suppliers/<int:supplier_id>/edit/', views.supplier_edit, name='supplier_edit'),
    path('suppliers/<int:supplier_id>/delete/', views.supplier_delete, name='supplier_delete'),

    path('categories/', views.category_list, name='category_list'),
    path('categories/<int:category_id>/detail/', views.category_detail_api, name='category_detail'),
    path('categories/create/', views.category_create, name='category_create'),
    path('categories/<int:category_id>/edit/', views.category_edit, name='category_edit'),
    path('categories/<int:category_id>/delete/', views.category_delete, name='category_delete'),

    path('materials/', views.raw_material_list, name='material_list'),
    path('materials/<int:material_id>/detail/', views.raw_material_detail_api, name='material_detail'),
    path('materials/create/', views.raw_material_create, name='material_create'),
    path('materials/<int:material_id>/edit/', views.raw_material_edit, name='material_edit'),
    path('materials/<int:material_id>/delete/', views.raw_material_delete, name='material_delete'),

    path('movements/', views.stock_movement_list, name='movement_list'),
    path('movements/create/', views.stock_movement_create, name='movement_create'),
    path('production-queue/', views.production_issue_queue, name='production_issue_queue'),
    path('production-queue/<int:issue_id>/cancel/', views.cancel_material_issue, name='cancel_material_issue'),
    path('production-queue/<int:issue_id>/issue/', views.issue_material, name='issue_material'),

    path('purchase-orders/', views.purchase_order_list, name='purchase_order_list'),
    path('purchase-orders/<int:order_id>/detail/', views.purchase_order_detail_api, name='purchase_order_detail'),
    path('purchase-orders/create/', views.purchase_order_create, name='purchase_order_create'),
    path('purchase-orders/<int:order_id>/edit/', views.purchase_order_edit, name='purchase_order_edit'),
    path('purchase-orders/<int:order_id>/delete/', views.purchase_order_delete, name='purchase_order_delete'),
    path('purchase-orders/<int:order_id>/receive/', views.purchase_order_receive, name='purchase_order_receive'),
    path('purchase-orders/<int:order_id>/items/add/', views.purchase_order_item_add, name='purchase_order_item_add'),
    path('purchase-orders/items/<int:item_id>/delete/', views.purchase_order_item_delete, name='purchase_order_item_delete'),

    path('low-stock/', views.low_stock_report, name='low_stock_report'),
]
