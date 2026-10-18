from django.contrib import admin

from .models import Payment, Transaction


class TransactionInline(admin.TabularInline):
    model = Transaction
    extra = 0
    readonly_fields = ['amount', 'ref_id', 'card_pan', 'created_at']
    fields = ['amount', 'ref_id', 'card_pan', 'created_at']


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'amount', 'status', 'gateway', 'authority', 'paid_at', 'created_at']
    list_filter = ['status', 'gateway', 'created_at']
    search_fields = ['order__id', 'transaction_id', 'authority']
    readonly_fields = ['order', 'amount', 'status', 'gateway', 'transaction_id', 'authority', 'paid_at', 'created_at']
    inlines = [TransactionInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['id', 'payment', 'amount', 'ref_id', 'card_pan', 'created_at']
    list_filter = ['created_at']
    search_fields = ['payment__order__id', 'ref_id']
    readonly_fields = ['payment', 'amount', 'ref_id', 'card_pan', 'created_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False