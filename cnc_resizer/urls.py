from django.urls import path

from . import views

app_name = "cnc_resizer"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/process/", views.process, name="process"),
    path("api/archive-files/", views.archive_files, name="archive_files"),
]
