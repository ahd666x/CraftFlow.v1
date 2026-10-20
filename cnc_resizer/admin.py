# from django.contrib import admin

# from .models import CncControllerProfile, CncFile, CncPartProfile, CncPlanningIssue, CncSheetPlan


# @admin.register(CncFile)
# class CncFileAdmin(admin.ModelAdmin):
#     list_display = (
#         "barcode_key",
#         "kind",
#         "controller_profile",
#         "resizable",
#         "length_before",
#         "length_after",
#         "created_at",
#     )
#     list_filter = ("kind", "resizable", "controller_profile")
#     search_fields = ("barcode_key", "sha256", "path")
#     readonly_fields = ("sha256", "created_at", "validation_error")


# @admin.register(CncControllerProfile)
# class CncControllerProfileAdmin(admin.ModelAdmin):
#     list_display = ("key", "manufacturer", "model", "firmware_version", "adapter_key", "active", "approved", "approved_by", "approved_at")
#     list_filter = ("active", "approved", "manufacturer")
#     search_fields = ("key", "manufacturer", "model", "firmware_version")
#     readonly_fields = ("created_at", "updated_at")


# @admin.register(CncPartProfile)
# class CncPartProfileAdmin(admin.ModelAdmin):
#     list_display = ("part", "source_file", "approved", "approved_by", "updated_at")
#     list_filter = ("approved", "updated_at")
#     search_fields = ("part__f3", "part__name")
#     autocomplete_fields = ("part", "source_file", "approved_by")
#     readonly_fields = ("created_at", "updated_at")


# @admin.register(CncSheetPlan)
# class CncSheetPlanAdmin(admin.ModelAdmin):
#     list_display = ("id", "material", "source_stock", "sheet_index", "status", "result_outcome", "completed_by", "completed_at", "created_at")
#     list_filter = ("status", "result_outcome", "material")
#     search_fields = ("material__name",)
#     readonly_fields = tuple(field.name for field in CncSheetPlan._meta.fields)

#     def has_add_permission(self, request):
#         return False

#     def has_delete_permission(self, request, obj=None):
#         return False


# @admin.register(CncPlanningIssue)
# class CncPlanningIssueAdmin(admin.ModelAdmin):
#     list_display = ("id", "task", "order", "part", "mode", "status", "resolved_by", "resolved_at", "created_by", "created_at")
#     list_filter = ("mode", "status", "created_at")
#     search_fields = ("reason", "part__name", "part__f3")
#     readonly_fields = ("created_at",)
