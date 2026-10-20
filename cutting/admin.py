# from django.contrib import admin
# from .models import CuttingSettings, CutPlan, CutPlanSheet, CutPlanTask, SheetSizeSpec, SheetStock, SheetStockEvent


# @admin.register(SheetSizeSpec)
# class SheetSizeSpecAdmin(admin.ModelAdmin):
#     list_display = ('material', 'name', 'width', 'height', 'active', 'is_default')
#     list_filter = ('material', 'active', 'is_default')
#     search_fields = ('material__name', 'name')


# @admin.register(SheetStock)
# class SheetStockAdmin(admin.ModelAdmin):
#     list_display = ('material', 'width', 'height', 'quantity', 'is_offcut', 'active', 'priority', 'created_at')
#     list_filter = ('material', 'is_offcut', 'active')
#     search_fields = ('material__name', 'note', 'inactive_reason')
#     readonly_fields = ('created_at', 'updated_at')


# @admin.register(CuttingSettings)
# class CuttingSettingsAdmin(admin.ModelAdmin):
#     list_display = ('id', 'strategy', 'updated_by', 'updated_at')
#     readonly_fields = ('updated_by', 'updated_at')


# class CutPlanTaskInline(admin.TabularInline):
#     model = CutPlanTask
#     extra = 0


# class CutPlanSheetInline(admin.TabularInline):
#     model = CutPlanSheet
#     extra = 0
#     readonly_fields = ('source_stock', 'material', 'layout_index', 'width', 'height', 'was_offcut', 'outcome', 'actual_offcuts', 'result_note')


# @admin.register(CutPlan)
# class CutPlanAdmin(admin.ModelAdmin):
#     list_display = ('id', 'status', 'created_by', 'created_at', 'cut_at')
#     list_filter = ('status', 'created_at')
#     readonly_fields = ('plan_data', 'created_at', 'cut_at', 'cancelled_at')
#     inlines = (CutPlanTaskInline, CutPlanSheetInline)


# @admin.register(SheetStockEvent)
# class SheetStockEventAdmin(admin.ModelAdmin):
#     list_display = ('created_at', 'event_type', 'material', 'width', 'height', 'is_offcut', 'quantity_delta', 'created_by', 'plan')
#     list_filter = ('event_type', 'material', 'is_offcut', 'created_at')
#     search_fields = ('material__name', 'note')
#     readonly_fields = tuple(field.name for field in SheetStockEvent._meta.fields)
