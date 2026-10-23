from django.urls import path
from . import views
from . import workspace_views

app_name = 'cutting'
urlpatterns = [
    path('', views.planner, name='planner'),
    path('preview/', views.planner_preview, name='planner_preview'),
    path('workspace/orders/', workspace_views.workspace_orders, name='ws_orders'),
    path('workspace/orders/<int:order_id>/', workspace_views.workspace_order, name='ws_order'),
    path('workspace/orders/<int:order_id>/parts/', workspace_views.workspace_parts, name='ws_parts'),
    path('workspace/tasks/<int:task_id>/spec/', workspace_views.workspace_task_spec, name='ws_task_spec'),
    path('workspace/tasks/<int:task_id>/exclude/', workspace_views.workspace_task_exclude, name='ws_task_exclude'),
    path('workspace/tasks/<int:task_id>/include/', workspace_views.workspace_task_include, name='ws_task_include'),
    path('workspace/tasks/<int:task_id>/reset/', workspace_views.workspace_task_reset, name='ws_task_reset'),
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
