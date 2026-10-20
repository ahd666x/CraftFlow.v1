from django.urls import path

from . import views

app_name = 'instructions'

urlpatterns = [
    path('', views.knowledge_browser, name='browser'),
    path('product/<int:pk>/', views.product_doc, name='product_doc'),
    path('product/<int:pk>/edit/', views.product_editor, name='product_editor'),
    path('product/<int:pk>/reorder/', views.product_reorder, name='product_reorder'),
    path('order/<int:pk>/', views.order_doc, name='order_doc'),
    path('instruction/save/', views.instruction_save, name='instruction_save'),
    path('instruction/<int:pk>/delete/', views.instruction_delete, name='instruction_delete'),
    path('instruction/<int:pk>/toggle/', views.instruction_toggle, name='instruction_toggle'),
    path('instruction/<int:pk>/image/add/', views.instruction_image_add, name='instruction_image_add'),
    path('instruction/image/<int:pk>/delete/', views.instruction_image_delete, name='instruction_image_delete'),
    path('checklist/state/<int:item_id>/', views.checklist_state, name='checklist_state'),
    path('checklist/toggle/', views.checklist_toggle, name='checklist_toggle'),
]
