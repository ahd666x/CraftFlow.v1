from django.contrib import admin

from .models import CncFile, CncPartProfile, CncPlanningIssue, CncSheetPlan


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


@admin.register(CncSheetPlan)
class CncSheetPlanAdmin(admin.ModelAdmin):
    list_display = ("id", "material", "source_stock", "sheet_index", "status", "result_outcome", "completed_by", "completed_at", "created_at")
    list_filter = ("status", "result_outcome", "material")
    search_fields = ("material__name",)
    readonly_fields = ("created_at",)


@admin.register(CncPlanningIssue)
class CncPlanningIssueAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "order", "part", "mode", "status", "resolved_by", "resolved_at", "created_by", "created_at")
    list_filter = ("mode", "status", "created_at")
    search_fields = ("reason", "part__name", "part__f3")
    readonly_fields = ("created_at",)
