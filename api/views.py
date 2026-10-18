from rest_framework import viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.contrib.auth import get_user_model

from product.models import (
    Product, ProductCategory, Order, ProductionTask,
)
from cart.models import Cart
from discounts.models import Discount
from accounts.models import UserProfile
from .serializers import (
    ProductSerializer, ProductCategorySerializer,
    CartSerializer, OrderSerializer,
    DiscountSerializer, UserSerializer, ProductionTaskSerializer,
)
from .permissions import IsAdminOrReadOnly

User = get_user_model()


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        return Response(UserSerializer(profile, context={'request': request}).data)


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.filter(is_active=True).select_related('category').order_by('-id')
    serializer_class = ProductSerializer
    permission_classes = [IsAdminOrReadOnly]


class ProductCategoryViewSet(viewsets.ModelViewSet):
    queryset = ProductCategory.objects.filter(is_active=True).order_by('-id')
    serializer_class = ProductCategorySerializer
    permission_classes = [IsAdminOrReadOnly]


class CartViewSet(viewsets.ModelViewSet):
    serializer_class = CartSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        cart, _ = Cart.objects.get_or_create(user=self.request.user)
        return cart

    def list(self, request, *args, **kwargs):
        cart = self.get_object()
        serializer = self.get_serializer(cart)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        cart = self.get_object()
        serializer = self.get_serializer(cart)
        return Response(serializer.data)


class OrderViewSet(viewsets.ModelViewSet):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).prefetch_related('items__product')


class DiscountViewSet(viewsets.ModelViewSet):
    queryset = Discount.objects.filter(is_active=True)
    serializer_class = DiscountSerializer
    permission_classes = [IsAdminOrReadOnly]


class ProductionTaskViewSet(viewsets.ModelViewSet):
    queryset = ProductionTask.objects.all()
    serializer_class = ProductionTaskSerializer
    permission_classes = [IsAdminOrReadOnly]


def api_root(request, format=None):
    from rest_framework.routers import DefaultRouter
    router = DefaultRouter()
    router.register('products', ProductViewSet, basename='product')
    router.register('categories', ProductCategoryViewSet, basename='category')
    router.register('cart', CartViewSet, basename='cart')
    router.register('orders', OrderViewSet, basename='order')
    router.register('discounts', DiscountViewSet, basename='discount')
    router.register('production-tasks', ProductionTaskViewSet, basename='production-task')
    return Response(router.get_api_view(request).data)