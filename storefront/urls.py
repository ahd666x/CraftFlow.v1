from django.urls import path, register_converter

from storefront.converters import UnicodeSlugConverter
from . import views
from .returns_views import (
    ReturnRequestListView,
    ReturnRequestCreateView,
    ReturnRequestDetailView,
)

register_converter(UnicodeSlugConverter, 'uslug')

app_name = 'storefront'

urlpatterns = [
    path('', views.home, name='home'),
    path('products/', views.product_list, name='product_list'),
    path('products/<uslug:slug>/', views.product_detail, name='product_detail'),
    path('categories/', views.category_list, name='category_list'),
    path('categories/<uslug:slug>/', views.category_detail, name='category_detail'),
    path('checkout/', views.checkout, name='checkout'),
    path('orders/', views.order_list, name='order_list'),
    path('orders/<int:order_id>/', views.order_detail, name='order_detail'),
    path('returns/', ReturnRequestListView.as_view(), name='return_list'),
    path('returns/create/<int:order_item_id>/', ReturnRequestCreateView.as_view(), name='return_create'),
    path('returns/<int:pk>/', ReturnRequestDetailView.as_view(), name='return_detail'),
]