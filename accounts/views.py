from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import AuthenticationForm
from django.urls import reverse_lazy
from django.views.generic import CreateView, TemplateView, UpdateView

from .forms import UserRegistrationForm, ProfileUpdateForm
from .models import UserProfile


class CustomLoginView(auth_views.LoginView):
    template_name = 'accounts/login.html'
    authentication_form = AuthenticationForm


class CustomLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy('accounts:login')


class RegisterView(CreateView):
    form_class = UserRegistrationForm
    template_name = 'accounts/register.html'
    success_url = reverse_lazy('accounts:login')


class ProfileView(TemplateView):
    template_name = 'accounts/profile.html'


class ProfileUpdateView(UpdateView):
    model = UserProfile
    form_class = ProfileUpdateForm
    template_name = 'accounts/profile_edit.html'
    success_url = reverse_lazy('accounts:profile')


class CustomPasswordResetView(auth_views.PasswordResetView):
    template_name = 'accounts/password_reset.html'


class CustomPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = 'accounts/password_reset_confirm.html'
