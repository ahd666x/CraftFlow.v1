"""
ادمین فروشگاه.

اینجا فقط مدل‌های اختصاصی لایه فروشگاه (نه مدل‌های `product`) ثبت می‌شوند.
"""
from django.contrib import admin

from .models import ProductReview, ComparisonList, Address, ReturnRequest, SiteSettings, HeroSlide


@admin.register(HeroSlide)
class HeroSlideAdmin(admin.ModelAdmin):
    list_display = ['title', 'order', 'is_active', 'created_at']
    list_filter = ['is_active']
    list_editable = ['order', 'is_active']
    search_fields = ['title', 'subtitle']
    ordering = ['order']
    fields = ['title', 'subtitle', 'image', 'accent_image', 'button_text', 'link_url', 'order', 'is_active']


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


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ['site_name', 'is_maintenance', 'shipping_cost', 'free_shipping_threshold']
    list_filter = ['is_maintenance']
    fieldsets = [
        ('عمومی', {'fields': ['site_name', 'logo', 'cover_image', 'is_maintenance']}),
        ('بخش هیرو', {'fields': ['hero_title', 'hero_subtitle']}),
        ('فوتر', {'fields': ['footer_text']}),
        ('ارسال و تحویل', {'fields': [
            'shipping_cost', 'free_shipping_threshold', 'tax_rate',
            'production_days_min', 'production_days_max',
            'shipping_days_min', 'shipping_days_max',
        ]}),
        ('نشان‌های اعتماد', {'fields': [
            'enamad_logo', 'enamad_url',
            'zarinpal_logo', 'zarinpal_url',
            'warranty_years',
        ]}),
    ]

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False