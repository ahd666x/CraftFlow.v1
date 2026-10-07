"""
ثبت پنل مدیریت Django برای ماژول انبار.

فقط مدل‌های واقعی Engine B ثبت شده‌اند. داده‌های تولیدی (صف روزانه و دفتر
گردش) عمداً قابل ویرایش دستی نیستند: هر نوشتنی باید از
``inventory.services`` بیاید تا قاعدهٔ بسته‌بندی و کسر مصرف در یک نقطه بماند.
"""
from django.contrib import admin
from django.utils.html import format_html

from .models import (
    DailyMaterialClosing,
    DailyMaterialQueue,
    DailyMaterialQueueSource,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)


@admin.register(RawMaterialCategory)
class RawMaterialCategoryAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(RawMaterial)
class RawMaterialAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'category', 'code', 'barcode', 'unit',
        'current_stock', 'pack_size', 'min_stock_alert', 'stock_status',
        'is_active',
    ]
    list_filter = ['category', 'unit', 'is_active']
    search_fields = ['name', 'code', 'barcode']
    readonly_fields = ['current_stock']

    @admin.display(description='موجودی')
    def current_stock(self, obj):
        return str(obj.current_stock)

    @admin.display(description='وضعیت موجودی')
    def stock_status(self, obj):
        badge = {'success': 'success', 'warning': 'warning',
                 'danger': 'danger'}[obj.stock_status]
        label = {'success': 'موجود', 'warning': 'کمبود',
                 'danger': 'تمام شده'}[obj.stock_status]
        return format_html('<span class="badge bg-{}">{}</span>', badge, label)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    """
    دفتر گردش فقط خواندنی است.

    اگر حرکتی از این پنل ساخته یا حذف شود، موجودی از گردش واقعی جدا می‌شود و
    هیچ حسابرسی این را نمی‌بیند؛ برای همین همهٔ فیلدها فقط‌خواندنی‌اند.
    """

    list_display = [
        'raw_material', 'movement_type', 'quantity', 'daily_queue',
        'reference_task', 'reference_order_item', 'created_by', 'created_at',
    ]
    list_filter = ['movement_type', 'created_at', 'raw_material__category']
    search_fields = ['raw_material__name', 'raw_material__code', 'note']
    date_hierarchy = 'created_at'
    readonly_fields = [f.name for f in StockMovement._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DailyMaterialQueue)
class DailyMaterialQueueAdmin(admin.ModelAdmin):
    list_display = [
        'work_date', 'worker', 'raw_material', 'planned_quantity',
        'delivered_quantity', 'returned_quantity', 'actual_consumption',
        'excess_consumption', 'status', 'has_plan_conflict',
    ]
    list_filter = [
        'work_date', 'status', 'has_plan_conflict', 'raw_material__category',
    ]
    search_fields = [
        'worker__first_name', 'worker__last_name', 'worker__username',
        'raw_material__name', 'raw_material__code',
    ]
    date_hierarchy = 'work_date'
    autocomplete_fields = ['worker', 'raw_material']
    readonly_fields = [
        'created_at', 'updated_at', 'computed_actual_consumption',
        'computed_excess_consumption',
        # نیاز از برنامهٔ تولید مشتق می‌شود و با Sync سرویس مرکزی به‌روز
        # می‌ماند؛ تحویل و بازگشت هم فقط از صف روزانه ثبت می‌شوند.
        'planned_quantity', 'delivered_quantity', 'returned_quantity',
        'actual_consumption', 'excess_consumption',
    ]


@admin.register(DailyMaterialQueueSource)
class DailyMaterialQueueSourceAdmin(admin.ModelAdmin):
    list_display = [
        'queue', 'kind', 'production_task', 'painting_stage', 'defect',
        'raw_material', 'quantity',
    ]
    list_filter = ['kind', 'queue__work_date', 'raw_material__category']
    search_fields = [
        'production_task__id', 'painting_stage__name', 'defect__description',
        'raw_material__name',
    ]
    autocomplete_fields = [
        'queue', 'production_task', 'painting_stage', 'defect', 'raw_material',
    ]


@admin.register(DailyMaterialClosing)
class DailyMaterialClosingAdmin(admin.ModelAdmin):
    list_display = ['work_date', 'status', 'closed_by', 'closed_at']
    list_filter = ['status', 'work_date']
    date_hierarchy = 'work_date'
    search_fields = ['closed_by__username', 'note']
    readonly_fields = [f.name for f in DailyMaterialClosing._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False