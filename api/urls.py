from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

from .views import (
    api_root,
    ProductViewSet,
    ProductCategoryViewSet,
    CartViewSet,
    OrderViewSet,
    DiscountViewSet,
    ProductionTaskViewSet,
    CurrentUserView,
)

app_name = 'api'

router = DefaultRouter()
router.register('products', ProductViewSet, basename='product')
router.register('categories', ProductCategoryViewSet, basename='category')
router.register('cart', CartViewSet, basename='cart')
router.register('orders', OrderViewSet, basename='order')
router.register('discounts', DiscountViewSet, basename='discount')
router.register('production-tasks', ProductionTaskViewSet, basename='production-task')

urlpatterns = [
    path('', api_root, name='root'),
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/swagger/', SpectacularSwaggerView.as_view(url='/api/v1/schema/'), name='swagger-ui'),
    path('docs/redoc/', SpectacularRedocView.as_view(url='/api/v1/schema/'), name='redoc'),
    path('auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/users/me/', CurrentUserView.as_view(), name='current-user'),
    path('', include(router.urls)),
]