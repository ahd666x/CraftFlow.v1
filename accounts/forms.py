from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import password_validation

from .models import UserProfile


def to_en_digits(value):
    """Convert Persian/Arabic digits to Latin digits."""
    if not value:
        return value
    trans = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
    return str(value).translate(trans)

User = get_user_model()


def _widget_class(field):
    """Return appropriate CSS class for widget type."""
    if isinstance(field.widget, forms.Select):
        return 'select'
    return 'input'


class LoginForm(AuthenticationForm):
    """فرم لاگین پیش‌فرض جنگو را با ویجت‌های استایل‌دار فروشگاه می‌پیچانیم."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({'class': _widget_class(field)})


class UserRegistrationForm(forms.Form):
    username = forms.CharField(
        max_length=150, label="نام کاربری",
        widget=forms.TextInput(attrs={'class': 'input'}),
    )
    email = forms.EmailField(
        label="ایمیل",
        widget=forms.EmailInput(attrs={'class': 'input'}),
    )
    phone = forms.CharField(
        max_length=15, label="شماره موبایل",
        widget=forms.TextInput(attrs={'class': 'input'}),
    )
    password1 = forms.CharField(
        label="کلمه عبور",
        widget=forms.PasswordInput(attrs={'class': 'input'}),
    )
    password2 = forms.CharField(
        label="تکرار کلمه عبور",
        widget=forms.PasswordInput(attrs={'class': 'input'}),
    )

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
        # Convert Persian/Arabic digits to Latin
        phone = to_en_digits(phone)
        if not phone.startswith('09') or len(phone) != 11:
            raise forms.ValidationError("شماره موبایل باید با ۰۹ شروع شود و ۱۱ رقم باشد.")
        return phone

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', "کلمات عبور مطابقت ندارند.")
        elif p1:
            try:
                password_validation.validate_password(p1)
            except forms.ValidationError as e:
                self.add_error('password1', e)
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
        widgets = {
            'phone': forms.TextInput(attrs={'class': 'input'}),
            'bio': forms.Textarea(attrs={'class': 'input', 'rows': 4}),
        }
