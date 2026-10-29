import json

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.generic import ListView, CreateView, UpdateView, View
from django.urls import reverse_lazy
from django.contrib import messages

from .models import Discount
from .forms import DiscountForm, ApplyDiscountForm
from cart.services import get_or_create_cart


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        return self.request.user.is_staff

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(self.request.get_full_path())
        raise PermissionDenied


class DiscountListView(StaffRequiredMixin, ListView):
    model = Discount
    template_name = 'discounts/discount_list.html'
    context_object_name = 'discounts'


class DiscountCreateView(StaffRequiredMixin, CreateView):
    model = Discount
    form_class = DiscountForm
    template_name = 'discounts/discount_form.html'
    success_url = reverse_lazy('discounts:discount_list')


class DiscountUpdateView(StaffRequiredMixin, UpdateView):
    model = Discount
    form_class = DiscountForm
    template_name = 'discounts/discount_form.html'
    success_url = reverse_lazy('discounts:discount_list')


def _hx_cart_response(request, cart, message):
    """پاسخ جزئی HTMX بعد از اعمال/حذف کد تخفیف.

    پیام با HX-Trigger (cartMessage) به toast می‌رود؛ سشن آلوده نمی‌شود
    تا پیام‌های کهنه روی صفحات بعدی ظاهر نشوند.
    """
    items = cart.items.select_related(
        'product', 'product__category',
    ).prefetch_related('product__gallery_images')
    if request.headers.get('HX-Target') == 'cart-rows':
        response = render(request, 'cart/_cart_page.html', {'cart': cart, 'items': items})
    else:
        response = render(request, 'cart/_mini_cart.html', {'cart': cart, 'items': items})
    response['HX-Trigger'] = json.dumps(
        {
            'cartMessage': message,
            'cartUpdated': {'count': cart.total_items, 'silent': True},
        },
        ensure_ascii=False,
    )
    return response


class ApplyDiscountView(View):
    """اعمال کد تخفیف — برای سبد مهمان هم باز است (سبد مهمان session-key دارد
    و بعد از ورود با merge_guest_cart_to_user منتقل می‌شود). فقط POST + CSRF."""

    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        cart = get_or_create_cart(request)
        form = ApplyDiscountForm(request.POST)
        message = None
        ok = False
        if form.is_valid():
            code = form.cleaned_data['code']
            try:
                discount = Discount.objects.get(code=code)
            except Discount.DoesNotExist:
                message = 'کد تخفیف نامعتبر است.'
            else:
                if not discount.is_valid:
                    message = 'کد تخفیف منقضی شده یا به حد نصاب رسیده است.'
                else:
                    cart.discount = discount
                    cart.save()
                    message = f'کد تخفیف {discount.code} با موفقیت اعمال شد.'
                    ok = True
        else:
            message = 'کد تخفیف را وارد کنید.'

        if request.headers.get('HX-Request'):
            return _hx_cart_response(request, cart, message)

        if ok:
            messages.success(request, message)
        else:
            messages.error(request, message)
        return redirect('cart:cart_detail')


class RemoveDiscountView(View):
    """حذف کد تخفیف — مثل اعمال، برای سبد مهمان هم مجاز."""

    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        cart = get_or_create_cart(request)
        cart.discount = None
        cart.save()
        message = 'کد تخفیف حذف شد.'

        if request.headers.get('HX-Request'):
            return _hx_cart_response(request, cart, message)

        messages.success(request, message)
        return redirect('cart:cart_detail')
