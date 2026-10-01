from django.urls import path

from craftflow_ai import views

app_name = 'craftflow_ai'

urlpatterns = [
    path('', views.chat_view, name='chat'),
    path('api/chat/', views.chat_api, name='chat_api'),
    path('api/tools/', views.tools_api, name='tools_api'),
    path('api/history/<str:conversation_id>/', views.chat_history, name='chat_history'),
]