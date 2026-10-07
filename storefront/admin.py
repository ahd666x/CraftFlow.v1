"""
ادمین فروشگاه.

اینجا فقط مدل‌های اختصاصی لایه فروشگاه (نه مدل‌های `product`) ثبت می‌شوند.
"""
from django.contrib import admin

from .models import ColorOption, ProductImage, ProductReview, ComparisonList, Address, ReturnRequest


@admin.register(ColorOption)
class ColorOptionAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'code']


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ['product', 'color', 'is_active']
    list_filter = ['is_active', 'color']
    search_fields = ['product__name']


@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ['product', 'user', 'rating', 'is_active']
    list_filter = ['rating', 'is_active']
    search_fields = ['product__name', 'user__username', 'comment']


@admin.register(ComparisonList)
class ComparisonListAdmin(admin.ModelAdmin):
    list_display = ['session_key', 'is_active']
    list_filter = ['is_active']


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ['user', 'title', 'city', 'is_default', 'is_active']
    list_filter = ['is_active', 'is_default']
    search_fields = ['user__username', 'title', 'city']


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ['order_item', 'user', 'status', 'created_at']
    list_filter = ['status']
    search_fields = ['user__username', 'reason']
    actions = ['approve_requests', 'reject_requests']

    def approve_requests(self, request, queryset):
        for obj in queryset:
            obj.approve()
    approve_requests.short_description = 'تایید درخواست‌های انتخاب‌شده'

    def reject_requests(self, request, queryset):
        for obj in queryset:
            obj.reject()
    reject_requests.short_description = 'رد درخواست‌های انتخاب‌شده'
