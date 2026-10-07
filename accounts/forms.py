from django import forms
from django.contrib.auth import get_user_model

from .models import UserProfile

User = get_user_model()


class UserRegistrationForm(forms.Form):
    username = forms.CharField(max_length=150, label="نام کاربری")
    email = forms.EmailField(label="ایمیل")
    phone = forms.CharField(max_length=15, label="شماره موبایل")
    password1 = forms.CharField(label="کلمه عبور", widget=forms.PasswordInput)
    password2 = forms.CharField(label="تکرار کلمه عبور", widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("این نام کاربری قبلاً ثبت شده است.")
        return username

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("این ایمیل قبلاً ثبت شده است.")
        return email

    def clean_phone(self):
        phone = self.cleaned_data['phone']
        if not phone.startswith('09') or len(phone) != 11:
            raise forms.ValidationError("شماره موبایل باید با ۰۹ شروع شود و ۱۱ رقم باشد.")
        return phone

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("کلمات عبور مطابقت ندارند.")
        return cleaned

    def save(self, commit=True):
        user = User(
            username=self.cleaned_data['username'],
            email=self.cleaned_data['email'],
        )
        user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
        return user


class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['phone', 'bio']
        labels = {
            'phone': 'شماره موبایل',
            'bio': 'درباره',
        }