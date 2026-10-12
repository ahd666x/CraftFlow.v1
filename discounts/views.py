from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
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


class ApplyDiscountView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        cart = get_or_create_cart(request)
        form = ApplyDiscountForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['code']
            try:
                discount = Discount.objects.get(code=code)
            except Discount.DoesNotExist:
                messages.error(request, 'کد تخفیف نامعتبر است.')
                return redirect('cart:cart_detail')

            if not discount.is_valid:
                messages.error(request, 'کد تخفیف منقضی شده یا به حد نصاب رسیده است.')
                return redirect('cart:cart_detail')

            cart.discount = discount
            cart.save()
            messages.success(request, f'کد تخفیف {discount.code} با موفقیت اعمال شد.')
        else:
            messages.error(request, 'کد تخفیف را وارد کنید.')
        return redirect('cart:cart_detail')


class RemoveDiscountView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        cart = get_or_create_cart(request)
        cart.discount = None
        cart.save()
        messages.success(request, 'کد تخفیف حذف شد.')
        return redirect('cart:cart_detail')