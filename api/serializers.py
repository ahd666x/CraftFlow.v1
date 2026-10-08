from rest_framework import serializers
from django.contrib.auth import get_user_model

from product.models import (
    Product, ProductCategory, Order, OrderItem, ProductionTask,
)
from storefront.models import ProductImage, ProductReview
from cart.models import Cart, CartItem
from discounts.models import Discount
from accounts.models import UserProfile

User = get_user_model()


class ProductCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ['id', 'name', 'slug']


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'alt_text']


class ProductReviewSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField()

    class Meta:
        model = ProductReview
        fields = ['id', 'user', 'rating', 'comment', 'is_active', 'created_at']


class ProductSerializer(serializers.ModelSerializer):
    category = ProductCategorySerializer(read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    reviews = ProductReviewSerializer(many=True, read_only=True)
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'category', 'name', 'slug', 'color', 'description',
            'default_size', 'default_colors', 'base_price',
            'length_price_percent', 'width_price_percent', 'height_price_percent',
            'length_editable', 'width_editable', 'height_editable',
            'length', 'width', 'height',
            'images', 'reviews', 'average_rating', 'review_count',
            'created_at', 'updated_at'
        ]


class ProductListSerializer(serializers.ModelSerializer):
    category = ProductCategorySerializer(read_only=True)
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'category', 'name', 'slug', 'color', 'description',
            'base_price', 'average_rating', 'review_count',
            'created_at'
        ]


class CartItemSerializer(serializers.ModelSerializer):
    product = ProductListSerializer(read_only=True)
    product_id = serializers.IntegerField(write_only=True)
    total_price = serializers.IntegerField(read_only=True)

    class Meta:
        model = CartItem
        fields = ['id', 'product', 'product_id', 'quantity', 'total_price']


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total_items = serializers.IntegerField(read_only=True)
    total_price = serializers.IntegerField(read_only=True)
    discount_amount = serializers.IntegerField(read_only=True)
    final_price = serializers.IntegerField(read_only=True)

    class Meta:
        model = Cart
        fields = [
            'id', 'items', 'total_items', 'total_price',
            'discount_amount', 'final_price', 'created_at'
        ]


class OrderItemSerializer(serializers.ModelSerializer):
    product = ProductListSerializer(read_only=True)

    class Meta:
        model = OrderItem
        fields = ['id', 'product', 'quantity', 'unit_price', 'notes', 'size']


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    user = serializers.StringRelatedField()

    class Meta:
        model = Order
        fields = [
            'id', 'user', 'customer', 'number', 'created_at', 'due_date',
            'priority', 'status', 'total_amount', 'discount_amount',
            'final_amount', 'shipping_address', 'tracking_code', 'paid_at'
        ]


class DiscountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Discount
        fields = [
            'id', 'code', 'discount_type', 'value', 'max_uses',
            'used_count', 'valid_from', 'valid_until', 'is_active'
        ]


class UserSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)

    class Meta:
        model = UserProfile
        fields = ['id', 'username', 'email', 'phone', 'bio']


class ProductionTaskSerializer(serializers.ModelSerializer):
    station_display = serializers.CharField(source='get_station_name_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = ProductionTask
        fields = [
            'id', 'order', 'station_name', 'station_display', 'status', 'status_display',
            'quantity', 'scanned_by', 'completed_at', 'step_order', 'assigned_worker'
        ]