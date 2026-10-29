from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth import login
from django.db.models import Sum
from django.urls import reverse, reverse_lazy
from django.views.generic import FormView, TemplateView, UpdateView, DeleteView, ListView, CreateView
from django.shortcuts import get_object_or_404, redirect

from .forms import (
    LoginForm, UserRegistrationForm, ProfileUpdateForm,
    AddressForm, StyledPasswordChangeForm,
    StyledPasswordResetForm, StyledSetPasswordForm,
)
from .models import UserProfile
from storefront.models import Address
from storefront.permissions import can_see_prices
from product.models import Order
from cart.services import merge_guest_cart_to_user

REPRESENTATIVE_GROUP = 'نماینده'


class CustomLoginView(auth_views.LoginView):
    template_name = 'accounts/login.html'
    authentication_form = LoginForm

    def get_success_url(self):
        next_url = self.get_redirect_url()
        if next_url:
            return next_url
        user = self.request.user
        if (not user.is_superuser
                and user.groups.filter(name=REPRESENTATIVE_GROUP).exists()
                and not user.groups.filter(name__in=['1', '2', '3', 'انبار']).exists()):
            return reverse('customer_order_list')  # /customer/orders/
        return super().get_success_url()

    def form_valid(self, form):
        # کلید سشن مهمان را باید *قبل از* login() نگه داشت؛ Django در
        # login() با cycle_key() کلید سشن را عوض می‌کند و بعد از آن سبدِ
        # گره‌خورده به کلید قدیمی دیگر قابل پیدا کردن نیست (merge بی‌صدا
        # هیچ می‌شد).
        guest_session_key = self.request.session.session_key
        response = super().form_valid(form)
        # Merge guest cart into user cart after successful login
        merge_guest_cart_to_user(
            self.request, self.request.user, guest_session_key=guest_session_key,
        )
        return response


class CustomLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy('accounts:login')


class RegisterView(FormView):
    form_class = UserRegistrationForm
    template_name = 'accounts/register.html'
    success_url = reverse_lazy('storefront:home')

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.success_url)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.save()
        UserProfile.objects.update_or_create(
            user=user, defaults={'phone': form.cleaned_data['phone']},
        )
        login(self.request, user)
        messages.success(self.request, 'حساب شما ساخته شد.')
        return super().form_valid(form)


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'accounts/profile.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        context['profile_user'] = profile

        # Recent order
        recent_order = Order.objects.filter(user=self.request.user).select_related('customer').order_by('-id').first()
        context['recent_order'] = recent_order

        # Default address
        default_address = Address.objects.filter(user=self.request.user, is_default=True, is_active=True).first()
        context['default_address'] = default_address

        # Stats
        context['order_count'] = Order.objects.filter(user=self.request.user).count()
        context['address_count'] = Address.objects.filter(user=self.request.user, is_active=True).count()

        # Prices — مثل بقیهٔ فروشگاه، مبلغ‌ها فقط برای کاربر با مجوز قیمت.
        # قبلاً کل خریدها و مبلغ سفارش اخیر برای هر کاربر لاگین‌شده نمایش
        # داده می‌شد (نشت قیمت برای نماینده‌های بی‌مجوز).
        show_prices = can_see_prices(self.request.user)
        context['show_prices'] = show_prices
        if show_prices:
            total_spent = Order.objects.filter(user=self.request.user).aggregate(total=Sum('final_amount'))['total']
            context['total_spent'] = total_spent or 0

        return context


class ProfileUpdateView(LoginRequiredMixin, UpdateView):
    model = UserProfile
    form_class = ProfileUpdateForm
    template_name = 'accounts/profile_edit.html'
    success_url = reverse_lazy('accounts:profile')

    def get_object(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        return profile


class AddressListView(LoginRequiredMixin, ListView):
    model = Address
    template_name = 'accounts/address_list.html'
    context_object_name = 'addresses'

    def get_queryset(self):
        return Address.objects.filter(user=self.request.user, is_active=True)


class AddressCreateView(LoginRequiredMixin, CreateView):
    model = Address
    template_name = 'accounts/address_form.html'
    form_class = AddressForm
    success_url = reverse_lazy('accounts:address_list')

    def form_valid(self, form):
        form.instance.user = self.request.user
        if form.instance.is_default:
            Address.objects.filter(user=self.request.user, is_default=True).update(is_default=False)
        messages.success(self.request, 'آدرس با موفقیت اضافه شد.')
        return super().form_valid(form)


class AddressUpdateView(LoginRequiredMixin, UpdateView):
    model = Address
    template_name = 'accounts/address_form.html'
    form_class = AddressForm
    success_url = reverse_lazy('accounts:address_list')

    def get_queryset(self):
        return Address.objects.filter(user=self.request.user, is_active=True)

    def form_valid(self, form):
        if form.instance.is_default:
            Address.objects.filter(user=self.request.user, is_default=True).exclude(pk=form.instance.pk).update(is_default=False)
        messages.success(self.request, 'آدرس با موفقیت به‌روزرسانی شد.')
        return super().form_valid(form)


class AddressDeleteView(LoginRequiredMixin, DeleteView):
    """حذف نرم آدرس (is_active=False).

    DeleteView پیش‌فرض رکورد را فیزیکی پاک می‌کرد و چون Order.address با
    SET_NULL تعریف شده، سفارش‌های قبلی بدون ردپای آدرس می‌ماندند. حذف نرم
    تاریخچهٔ سفارش را حفظ می‌کند و از لیست کاربر هم محو می‌شود.
    """
    model = Address
    template_name = 'accounts/address_confirm_delete.html'
    success_url = reverse_lazy('accounts:address_list')

    def get_queryset(self):
        return Address.objects.filter(user=self.request.user, is_active=True)

    def form_valid(self, form):
        self.object.is_active = False
        self.object.is_default = False
        self.object.save(update_fields=['is_active', 'is_default', 'updated_at'])
        messages.success(self.request, 'آدرس حذف شد.')
        return redirect(self.get_success_url())


class CustomPasswordResetView(auth_views.PasswordResetView):
    template_name = 'accounts/password_reset.html'
    form_class = StyledPasswordResetForm


class CustomPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = 'accounts/password_reset_confirm.html'
    form_class = StyledSetPasswordForm


class CustomPasswordChangeView(auth_views.PasswordChangeView):
    """تغییر رمز عبور پنل حساب.

    قبلاً قالب 'accounts/password_change.html' وجود نداشت و کلیک روی
    «تغییر کلمه عبور» در پروفایل خطای 500 (TemplateDoesNotExist) می‌داد.
    """
    template_name = 'accounts/password_change.html'
    form_class = StyledPasswordChangeForm
    success_url = reverse_lazy('accounts:password_change_done')


class CustomPasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = 'accounts/password_change_done.html'