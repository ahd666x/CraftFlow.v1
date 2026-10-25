"""Django admin for the CNC module.

bugfix: this file used to be fully commented out while
``register_cnc_controller`` and the README direct operators to approve
controller profiles in the admin — with nothing registered, «تأیید
حسابرسی‌شده» had no surface at all. All models are registered now; the
``CncControllerProfile.approved`` flag is wired into the machine-output
gate in ``controller.py`` (an active, unapproved profile blocks output).
"""
# from django.contrib import admin
# from django.urls import reverse
# from django.utils.html import format_html

# from .models import (
#     CncControllerProfile,
#     CncEngineeringProfile,
#     CncFile,
#     CncPartProfile,
#     CncPartTemplate,
#     CncPlanningIssue,
#     CncSheetPlan,
#     EditTemplate,
#     ToolSet,
#     ToolSlot,
# )


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
#     list_display = ("key", "manufacturer", "model", "firmware_version", "adapter_key",
#                     "active", "approved", "approved_by", "approved_at")
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
#     list_display = ("id", "material", "source_stock", "sheet_index", "status", "result_outcome", "completed_by", "completed_at", "created_at", "open_sheet_planner")
#     list_filter = ("status", "result_outcome", "material")
#     search_fields = ("material__name",)
#     readonly_fields = tuple(field.name for field in CncSheetPlan._meta.fields)

#     @admin.display(description="برنامه‌های ورق CNC")
#     def open_sheet_planner(self, obj):
#         """لینک صفحهٔ برنامه‌ریزی ورق از پنل ادمین — لینک اپراتور در ادیتور فعلاً کامنت است."""
#         return format_html(
#             '<a href="{}" target="_blank">باز کردن صفحهٔ برنامه‌های ورق CNC ←</a>',
#             reverse("cnc_resizer:sheet_plans"),
#         )

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


# @admin.register(ToolSlot)
# class ToolSlotAdmin(admin.ModelAdmin):
#     list_display = ("slot", "label", "dia", "updated_at")
#     search_fields = ("label",)


# @admin.register(ToolSet)
# class ToolSetAdmin(admin.ModelAdmin):
#     list_display = ("name", "created_at", "updated_at")
#     search_fields = ("name",)


# @admin.register(EditTemplate)
# class EditTemplateAdmin(admin.ModelAdmin):
#     list_display = ("name", "created_by", "created_at", "updated_at")
#     search_fields = ("name",)
#     readonly_fields = ("created_at", "updated_at")


# @admin.register(CncPartTemplate)
# class CncPartTemplateAdmin(admin.ModelAdmin):
#     list_display = ("name", "part", "source_feature_count", "created_by", "updated_at")
#     list_filter = ("part",)
#     search_fields = ("name", "part__name", "part__f3")
#     readonly_fields = ("created_at", "updated_at")


# @admin.register(CncEngineeringProfile)
# class CncEngineeringProfileAdmin(admin.ModelAdmin):
#     list_display = ("owner", "filename", "source_sha256", "updated_at")
#     search_fields = ("owner__username", "filename")
#     readonly_fields = ("created_at", "updated_at")
