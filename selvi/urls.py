from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.contrib.auth import views as auth_views
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from django.views.static import serve

from storefront.sitemaps import (
    StaticViewSitemap,
    ProductSitemap,
    CategorySitemap,
    HeroSlideSitemap,
)

# Error handlers
handler404 = 'storefront.views.custom_404'
handler500 = 'storefront.views.custom_500'

sitemaps = {
    'static': StaticViewSitemap,
    'products': ProductSitemap,
    'categories': CategorySitemap,
    'hero_slides': HeroSlideSitemap,
}

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path(
        'logout/',
        auth_views.LogoutView.as_view(),
        name='logout'
    ),
    re_path(
        r'^media/(?P<path>.*)$',
        serve,
        {'document_root': settings.MEDIA_ROOT}
    ),
    path('', include('product.urls')),
    path('inventory/', include('inventory.urls')),
    path('cutting/', include('cutting.urls')),
    path('craftflow-ai/', include('craftflow_ai.urls')),
    # فروشگاه روی پیشوند /shop سوار می‌شود تا با پنل تولید قاطی نشود.
    # قالب‌های فروشگاه هم namespace مستقل `storefront/` دارند.
    path('shop/', include('storefront.urls')),
    path('cart/', include('cart.urls')),
    path('discounts/', include('discounts.urls')),
    path('payments/', include('payments.urls')),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),
    path('robots.txt', TemplateView.as_view(template_name='robots.txt', content_type='text/plain')),
]
