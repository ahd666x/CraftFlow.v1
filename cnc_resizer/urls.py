from django.urls import path

from . import views

app_name = "cnc_resizer"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/process/", views.process, name="process"),
    path("api/analyze/", views.analyze, name="analyze"),
    path("api/archive-files/", views.archive_files, name="archive_files"),
    path("api/part-profile/", views.part_profile, name="part_profile"),
    path("sheets/", views.sheet_plans, name="sheet_plans"),
    path("sheets/<int:plan_id>/cancel/", views.cancel_sheet_plan, name="cancel_sheet_plan"),
    path("sheets/<int:plan_id>/download/", views.download_sheet_plan, name="download_sheet_plan"),
]
