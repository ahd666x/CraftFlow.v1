from django.urls import path

from . import views

app_name = 'payments'

urlpatterns = [
    path('create/<int:order_id>/', views.payment_create, name='payment_create'),
    path('verify/', views.payment_verify, name='payment_verify'),
]