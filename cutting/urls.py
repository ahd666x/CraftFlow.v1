from django.urls import path
from . import views

app_name = 'cutting'
urlpatterns = [
    path('', views.planner, name='planner'),
    path('preview/', views.planner_preview, name='planner_preview'),
    path('plans/', views.plan_list, name='plans'),
    path('plans/<int:plan_id>/', views.plan_detail, name='plan_detail'),
    path('plans/<int:plan_id>/download/', views.download_cut, name='download_cut'),
    path('plans/<int:plan_id>/mark-cut/', views.mark_cut, name='mark_cut'),
    path('plans/<int:plan_id>/cancel/', views.cancel_plan, name='cancel_plan'),
    path('inventory/', views.inventory, name='inventory'),
    path('inventory/full/', views.inventory, {'kind': 'full'}, name='full_sheets'),
    path('inventory/offcuts/', views.inventory, {'kind': 'offcuts'}, name='offcuts'),
    path('inventory/new/', views.stock_create, name='stock_create'),
    path('inventory/<int:stock_id>/edit/', views.stock_edit, name='stock_edit'),
    path('inventory/<int:stock_id>/deactivate/', views.stock_deactivate, name='stock_deactivate'),
    path('inventory/<int:stock_id>/reactivate/', views.stock_reactivate, name='stock_reactivate'),
    path('inventory/sizes/', views.size_specs, name='size_specs'),
    path('inventory/sizes/<int:spec_id>/edit/', views.size_specs, name='size_specs_edit'),
    path('inventory/policy/', views.cutting_policy, name='policy'),
    path('inventory/history/', views.stock_history, name='history'),
]
