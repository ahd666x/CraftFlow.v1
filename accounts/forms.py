from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm, PasswordChangeForm, PasswordResetForm, SetPasswordForm,
)
from django.contrib.auth import password_validation

from .models import UserProfile
from storefront.models import Address


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


def _style_form_fields(form):
    """به همهٔ فیلدهای فرم کلاس input/select فروشگاه را می‌دهد."""
    for field in form.fields.values():
        field.widget.attrs.update({'class': _widget_class(field)})


class LoginForm(AuthenticationForm):
    """فرم لاگین پیش‌فرض جنگو را با ویجت‌های استایل‌دار فروشگاه می‌پیچانیم."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style_form_fields(self)


class StyledPasswordChangeForm(PasswordChangeForm):
    """تغییر رمز عبور با ظاهر فروشگاه (بدون آن قالب -input- بدون استایل می‌ماند)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style_form_fields(self)


class StyledPasswordResetForm(PasswordResetForm):
    """فرم بازیابی رمز با ظاهر فروشگاه."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style_form_fields(self)


class StyledSetPasswordForm(SetPasswordForm):
    """تنظیم رمز جدید (بازیابی) با ظاهر فروشگاه."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style_form_fields(self)


class AddressForm(forms.ModelForm):
    """فرم آدرس با ویجت‌های استایل‌دار.

    قبل از این، CreateView/UpdateView با fields=[...] فرم می‌ساخت و
    ورودی‌ها بدون کلاس .input رندر می‌شدند — ناهماهنگ با بقیهٔ فروشگاه.
    """

    class Meta:
        model = Address
        fields = ['title', 'recipient', 'province', 'city', 'postal_code', 'address', 'is_default']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'input'}),
            'recipient': forms.TextInput(attrs={'class': 'input'}),
            'province': forms.TextInput(attrs={'class': 'input'}),
            'city': forms.TextInput(attrs={'class': 'input'}),
            'postal_code': forms.TextInput(attrs={
                'class': 'input', 'inputmode': 'numeric', 'dir': 'ltr',
                'maxlength': 10,
            }),
            'address': forms.Textarea(attrs={'class': 'input', 'rows': 3}),
            'is_default': forms.CheckboxInput(attrs={
                'class': 'h-4 w-4 accent-primary-600',
            }),
        }


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
            'phone': forms.TextInput(attrs={
                'class': 'input', 'inputmode': 'numeric', 'dir': 'ltr',
                'placeholder': '09123456789',
            }),
            'bio': forms.Textarea(attrs={'class': 'input', 'rows': 4}),
        }

    def clean_phone(self):
        """مثل ثبت‌نام: ارقام فارسی/عربی → لاتین و اعتبارسنجی 09 + ۱۱ رقم.

        قبلاً هیچ اعتبارسنجی‌ای اینجا نبود و کاربر می‌توانست هر رشته‌ای
        (یا ارقام فارسی) ذخیره کند — ناسازگار با داده‌ای که ثبت‌نام می‌سازد.
        """
        phone = to_en_digits(self.cleaned_data.get('phone', ''))
        if phone:
            if not phone.startswith('09') or len(phone) != 11 or not phone.isdigit():
                raise forms.ValidationError(
                    "شماره موبایل باید با ۰۹ شروع شود و ۱۱ رقم باشد."
                )
        return phone
