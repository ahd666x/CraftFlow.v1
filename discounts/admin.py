from django.contrib import admin

from .models import Discount


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ['code', 'discount_type', 'value', 'max_uses', 'used_count', 'valid_from', 'valid_until', 'is_active', 'is_valid']
    list_filter = ['discount_type', 'is_active', 'valid_from', 'valid_until']
    search_fields = ['code']
    list_editable = ['is_active']
    readonly_fields = ['used_count', 'created_at', 'updated_at']
    fieldsets = [
        ('اطلاعات اصلی', {'fields': ['code', 'discount_type', 'value']}),
        ('محدودیت‌ها', {'fields': ['max_uses', 'used_count']}),
        ('بازه زمانی', {'fields': ['valid_from', 'valid_until']}),
        ('وضعیت', {'fields': ['is_active']}),
    ]

    def is_valid(self, obj):
        return obj.is_valid
    is_valid.boolean = True
    is_valid.short_description = 'اعتبار دارد'