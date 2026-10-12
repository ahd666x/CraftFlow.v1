from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, TemplateView, UpdateView, DeleteView, ListView
from django.shortcuts import get_object_or_404, redirect

from .forms import LoginForm, UserRegistrationForm, ProfileUpdateForm
from .models import UserProfile
from storefront.models import Address

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


class CustomLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy('accounts:login')


class RegisterView(CreateView):
    form_class = UserRegistrationForm
    template_name = 'accounts/register.html'
    success_url = reverse_lazy('accounts:login')


class ProfileView(TemplateView):
    template_name = 'accounts/profile.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        context['profile_user'] = profile
        return context


class ProfileUpdateView(UpdateView):
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
    fields = ['title', 'recipient', 'province', 'city', 'postal_code', 'address', 'is_default']
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
    fields = ['title', 'recipient', 'province', 'city', 'postal_code', 'address', 'is_default']
    success_url = reverse_lazy('accounts:address_list')

    def get_queryset(self):
        return Address.objects.filter(user=self.request.user, is_active=True)

    def form_valid(self, form):
        if form.instance.is_default:
            Address.objects.filter(user=self.request.user, is_default=True).exclude(pk=form.instance.pk).update(is_default=False)
        messages.success(self.request, 'آدرس با موفقیت به‌روزرسانی شد.')
        return super().form_valid(form)


class AddressDeleteView(LoginRequiredMixin, DeleteView):
    model = Address
    template_name = 'accounts/address_confirm_delete.html'
    success_url = reverse_lazy('accounts:address_list')

    def get_queryset(self):
        return Address.objects.filter(user=self.request.user, is_active=True)

    def form_valid(self, form):
        messages.success(self.request, 'آدرس حذف شد.')
        return super().form_valid(form)


class CustomPasswordResetView(auth_views.PasswordResetView):
    template_name = 'accounts/password_reset.html'


class CustomPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = 'accounts/password_reset_confirm.html'