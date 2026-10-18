from django.contrib import admin

from .models import Cart, CartItem


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    readonly_fields = ['unit_price', 'total_price', 'has_custom_dimensions']
    fields = ['product', 'quantity', 'custom_length', 'custom_width', 'custom_height', 'unit_price', 'total_price', 'has_custom_dimensions']


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'session_key', 'total_items', 'total_price', 'discount', 'final_price', 'created_at']
    list_filter = ['created_at', 'discount']
    search_fields = ['user__username', 'session_key']
    readonly_fields = ['total_items', 'total_price', 'discount_amount', 'shipping_cost', 'tax_amount', 'final_price', 'estimated_delivery_min', 'estimated_delivery_max']
    inlines = [CartItemInline]

    def has_add_permission(self, request):
        return False


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ['cart', 'product', 'quantity', 'unit_price', 'total_price', 'has_custom_dimensions']
    list_filter = ['product__category']
    search_fields = ['product__name', 'cart__user__username']
    readonly_fields = ['unit_price', 'total_price', 'has_custom_dimensions']