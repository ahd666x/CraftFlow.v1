from django.urls import path

from . import views

app_name = "cnc_resizer"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/process/", views.process, name="process"),
    path("api/analyze/", views.analyze, name="analyze"),
    path("api/engine/analyze/", views.engine_analyze, name="engine_analyze"),
    path("api/engine/preview/", views.engine_preview, name="engine_preview"),
    path("api/engine/profile/", views.engine_profile, name="engine_profile"),
    path("api/engine/preflight/", views.engine_preflight, name="engine_preflight"),
    path("api/engine/output/", views.engine_output, name="engine_output"),
    path("engine/", views.engine_workbench, name="engine_workbench"),
    path("api/archive-files/", views.archive_files, name="archive_files"),
    path("api/tools/", views.tools, name="tools"),
    path("api/tools/sets/", views.tool_sets, name="tool_sets"),
    path("api/edit-templates/", views.edit_templates, name="edit_templates"),
    path("api/edit-templates/<int:template_id>/delete/", views.edit_template_delete, name="edit_template_delete"),
    path("api/part-profile/", views.part_profile, name="part_profile"),
    path("sheets/", views.sheet_plans, name="sheet_plans"),
    path("sheets/<int:plan_id>/cancel/", views.cancel_sheet_plan, name="cancel_sheet_plan"),
    path("sheets/<int:plan_id>/complete/", views.complete_sheet_plan, name="complete_sheet_plan"),
    path("sheets/<int:plan_id>/download/", views.download_sheet_plan, name="download_sheet_plan"),
    path("issues/<int:issue_id>/status/", views.update_planning_issue, name="update_planning_issue"),
]
