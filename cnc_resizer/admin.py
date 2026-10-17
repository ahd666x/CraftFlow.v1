from django.contrib import admin

from .models import CncFile, CncPartProfile


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


@admin.register(CncPartProfile)
class CncPartProfileAdmin(admin.ModelAdmin):
    list_display = ("part", "source_file", "approved", "approved_by", "updated_at")
    list_filter = ("approved", "updated_at")
    search_fields = ("part__f3", "part__name")
    autocomplete_fields = ("part", "source_file", "approved_by")
    readonly_fields = ("created_at", "updated_at")
