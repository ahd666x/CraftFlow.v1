from django.urls import path

from . import views

app_name = 'instructions'

urlpatterns = [
    path('product/<int:pk>/', views.product_doc, name='product_doc'),
]
