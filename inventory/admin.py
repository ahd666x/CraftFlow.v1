from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Supplier, RawMaterialCategory, RawMaterial, StockMovement,
    PurchaseOrder, PurchaseOrderItem, MaterialCustody, MaterialCustodyReturn,
)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ['name', 'phone', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'phone']


@admin.register(RawMaterialCategory)
class RawMaterialCategoryAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(RawMaterial)
class RawMaterialAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'code', 'unit', 'current_stock', 'min_stock_alert', 'stock_status', 'is_active']
    list_filter = ['category', 'unit', 'is_active']
    search_fields = ['name', 'code']
    readonly_fields = ['current_stock']

    def stock_status(self, obj):
        badge_map = {'success': 'success', 'warning': 'warning', 'danger': 'danger'}
        label_map = {'success': 'موجود', 'warning': 'کمبود', 'danger': 'تمام شده'}
        status = obj.stock_status
        return format_html('<span class="badge bg-{}">{}</span>', badge_map[status], label_map[status])
    stock_status.short_description = 'وضعیت موجودی'


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ['raw_material', 'movement_type', 'quantity', 'unit_price', 'reference_task', 'supplier', 'created_by', 'created_at']
    list_filter = ['movement_type', 'created_at', 'raw_material__category']
    search_fields = ['raw_material__name', 'note']
    date_hierarchy = 'created_at'
    autocomplete_fields = ['raw_material', 'supplier', 'reference_task']


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'supplier', 'status', 'created_at', 'created_by']
    list_filter = ['status', 'created_at']
    search_fields = ['supplier__name']
    date_hierarchy = 'created_at'
    autocomplete_fields = ['supplier']


@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(admin.ModelAdmin):
    list_display = ['purchase_order', 'raw_material', 'quantity', 'unit_price', 'received_quantity']
    list_filter = ['purchase_order__status']
    search_fields = ['raw_material__name']


@admin.register(MaterialCustody)
class MaterialCustodyAdmin(admin.ModelAdmin):
    list_display = ['raw_material', 'held_by', 'quantity', 'updated_at']
    list_filter = ['raw_material__category', 'held_by']
    search_fields = ['raw_material__name', 'raw_material__code', 'held_by__username']
    autocomplete_fields = ['raw_material', 'held_by']


@admin.register(MaterialCustodyReturn)
class MaterialCustodyReturnAdmin(admin.ModelAdmin):
    list_display = ['custody', 'measured_quantity', 'quantity_before', 'delta', 'recorded_by', 'created_at']
    list_filter = ['raw_material__category', 'held_by', 'created_at']
    search_fields = ['raw_material__name', 'held_by__username', 'note']
    date_hierarchy = 'created_at'
