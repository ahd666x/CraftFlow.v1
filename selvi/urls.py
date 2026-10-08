from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import path, include, re_path
from django.contrib.auth import views as auth_views
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    re_path(r'^media/(?P<path>.*)$', login_required(serve), {'document_root': settings.MEDIA_ROOT}),
    path('', include('product.urls')),
    path('inventory/', include('inventory.urls')),
    path('craftflow-ai/', include('craftflow_ai.urls')),
    # فروشگاه روی پیشوند /shop سوار می‌شود تا با پنل تولید قاطی نشود.
    # قالب‌های فروشگاه هم namespace مستقل `storefront/` دارند.
    path('shop/', include('storefront.urls')),
    path('cart/', include('cart.urls')),
    path('discounts/', include('discounts.urls')),
    path('payments/', include('payments.urls')),
    path('api/v1/', include('api.urls')),
]
