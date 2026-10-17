from django.contrib import admin

from .models import CncFile


@admin.register(CncFile)
class CncFileAdmin(admin.ModelAdmin):
    list_display = (
        "barcode_key",
        "kind",
        "resizable",
        "length_before",
        "length_after",
        "created_at",
    )
    list_filter = ("kind", "resizable")
    search_fields = ("barcode_key", "sha256", "path")
    readonly_fields = ("sha256", "created_at", "validation_error")
