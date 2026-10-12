from django.test import TestCase
from django.contrib.auth.models import User
from django.test import Client

from cart.models import Cart, CartItem
from product.models import Product, ProductCategory


class CartDetailTemplateTests(TestCase):
    """پچ ۲: نام قالب سبد خرید باید cart/detail.html باشد."""

    def setUp(self):
        self.user = User.objects.create_user(username="buyer", password="pw")
        self.client = Client()
        self.client.login(username="buyer", password="pw")

    def test_cart_detail_get_returns_200(self):
        response = self.client.get('/cart/')
        self.assertEqual(response.status_code, 200)


class CartItemIsolationTests(TestCase):
    """پچ ۴: سبد باید بر اساس CartItem.pk کار کند، نه product_id."""

    def setUp(self):
        self.user_a = User.objects.create_user(username="a", password="pw")
        self.user_b = User.objects.create_user(username="b", password="pw")
        self.category = ProductCategory.objects.create(name="دسته")
        self.product = Product.objects.create(
            category=self.category, name="کالا", base_price=1000,
            length=100, width=60, height=40,
        )
        self.cart_a = Cart.objects.create(user=self.user_a)
        self.cart_b = Cart.objects.create(user=self.user_b)
        self.client = Client()

    def _make_two_rows(self, cart):
        item1 = CartItem.objects.create(
            cart=cart, product=self.product, quantity=2,
            custom_length=100, custom_width=60, custom_height=40,
        )
        item2 = CartItem.objects.create(
            cart=cart, product=self.product, quantity=3,
            custom_length=120, custom_width=70, custom_height=50,
        )
        item1.recalculate_price(); item1.save()
        item2.recalculate_price(); item2.save()
        return item1, item2

    def test_update_one_row_does_not_touch_other(self):
        item1, item2 = self._make_two_rows(self.cart_a)
        from cart.services import CartService
        CartService.update_quantity(self.cart_a, item1.id, 5)
        item1.refresh_from_db()
        item2.refresh_from_db()
        self.assertEqual(item1.quantity, 5)
        self.assertEqual(item2.quantity, 3)

    def test_remove_one_row_keeps_other(self):
        item1, item2 = self._make_two_rows(self.cart_a)
        from cart.services import CartService
        CartService.remove_item(self.cart_a, item1.id)
        self.assertFalse(CartItem.objects.filter(pk=item1.id).exists())
        self.assertTrue(CartItem.objects.filter(pk=item2.id).exists())

    def test_user_b_cannot_touch_user_a_items(self):
        item_a1, item_a2 = self._make_two_rows(self.cart_a)
        from cart.services import CartService
        # سعی در دسترسی به آیتم کاربر A از طریق کاربر B
        self.assertEqual(CartService.update_quantity(self.cart_b, item_a1.id, 9), 0)
        self.assertEqual(CartService.remove_item(self.cart_b, item_a1.id), 0)
        item_a1.refresh_from_db()
        self.assertEqual(item_a1.quantity, 2)

    def test_cart_update_endpoint_isolation(self):
        item1, item2 = self._make_two_rows(self.cart_a)
        self.client.login(username="a", password="pw")
        response = self.client.post(f'/cart/update/{item1.id}/', {'quantity': 4})
        self.assertEqual(response.status_code, 302)
        item1.refresh_from_db()
        item2.refresh_from_db()
        self.assertEqual(item1.quantity, 4)
        self.assertEqual(item2.quantity, 3)

    def test_cart_remove_endpoint_isolation(self):
        item1, item2 = self._make_two_rows(self.cart_a)
        self.client.login(username="a", password="pw")
        response = self.client.post(f'/cart/remove/{item1.id}/')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CartItem.objects.filter(pk=item1.id).exists())
        self.assertTrue(CartItem.objects.filter(pk=item2.id).exists())


class DimensionLockTests(TestCase):
    """پچ ۵: ابعاد غیرقابل‌تغییر در سرور هم قفل می‌شوند."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username="buyer", password="pw")
        self.category = ProductCategory.objects.create(name="دسته")
        self.product = Product.objects.create(
            category=self.category, name="کالا", base_price=1000,
            length=100, width=60, height=40,
            length_editable=False, width_editable=True, height_editable=True,
        )
        self.cart = Cart.objects.create(user=self.user)

    def test_non_editable_length_ignores_client_value(self):
        from cart.services import CartService
        item = CartService.add_item(
            self.cart, self.product.id, quantity=1, length=999, width=70, height=50,
        )
        self.assertEqual(item.custom_length, 100)

    def test_zero_or_out_of_range_raises(self):
        from cart.services import CartService
        with self.assertRaises(ValueError):
            CartService.add_item(
                self.cart, self.product.id, quantity=1, length=100, width=0, height=50,
            )
        with self.assertRaises(ValueError):
            CartService.add_item(
                self.cart, self.product.id, quantity=1, length=100, width=5000, height=50,
            )

    def test_create_order_carries_locked_dimensions(self):
        from cart.services import CartService
        from storefront.views import _create_order
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.auth.models import User

        CartService.add_item(
            self.cart, self.product.id, quantity=1, length=999, width=70, height=50,
        )
        factory = RequestFactory()
        request = factory.post('/checkout/', {
            'shipping_address': 'آدرس', 'name': 'خریدار', 'phone': '0912',
        })
        setattr(request, 'session', 'session')
        setattr(request, '_messages', FallbackStorage(request))
        request.user = self.user
        _create_order(request, self.cart, list(self.cart.items.all()))
        from product.models import OrderItem
        item = OrderItem.objects.get(product=self.product)
        self.assertEqual(item.length, 100)
        self.assertEqual(item.width, 70)
        self.assertEqual(item.height, 50)