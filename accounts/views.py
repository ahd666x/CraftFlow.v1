from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, TemplateView, UpdateView
from django.shortcuts import get_object_or_404

from .forms import UserRegistrationForm, ProfileUpdateForm
from .models import UserProfile

REPRESENTATIVE_GROUP = 'نماینده'


class CustomLoginView(auth_views.LoginView):
    template_name = 'accounts/login.html'
    authentication_form = AuthenticationForm

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


class CustomPasswordResetView(auth_views.PasswordResetView):
    template_name = 'accounts/password_reset.html'


class CustomPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = 'accounts/password_reset_confirm.html'