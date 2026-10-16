from django.urls import path
from . import views
app_name = 'cutting'
urlpatterns = [
    path('', views.planner, name='planner'),
    path('plans/<int:plan_id>/', views.plan_detail, name='plan_detail'),
    path('plans/<int:plan_id>/download/', views.download_cut, name='download_cut'),
    path('plans/<int:plan_id>/mark-cut/', views.mark_cut, name='mark_cut'),
]
