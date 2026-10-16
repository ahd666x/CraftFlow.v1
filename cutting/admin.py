from django.contrib import admin
from .models import SheetStock, CutPlan

@admin.register(SheetStock)
class SheetStockAdmin(admin.ModelAdmin):
    list_display=('material','width','height','quantity','is_offcut','created_at')
    list_filter=('material','is_offcut')
    search_fields=('material__name',)

@admin.register(CutPlan)
class CutPlanAdmin(admin.ModelAdmin):
    list_display=('id','status','created_by','created_at','cut_at')
    list_filter=('status','created_at')
    readonly_fields=('plan_data','created_at','cut_at')
