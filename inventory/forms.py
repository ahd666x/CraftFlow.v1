"""
فرم‌های پنل انبار.

فقط فرم‌های تنظیمات باقی مانده‌اند: دستهٔ مواد و خود ماده. هیچ فرم عمومی
«ثبت گردش انبار» وجود ندارد، چون مصرف فقط از صف روزانه ثبت می‌شود و داشتن
فرم عمومی راه تازه‌ای برای کسر دوبارهٔ موجودی باز می‌کرد.
"""
from django import forms
from django.core.exceptions import ValidationError

from .models import RawMaterial, RawMaterialCategory


class RawMaterialCategoryForm(forms.ModelForm):
    class Meta:
        model = RawMaterialCategory
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'نام دسته (مثال: رنگ، آستر، یراق‌آلات)',
            }),
        }


class RawMaterialForm(forms.ModelForm):
    """
    تعریف مادهٔ اولیه.

    ``barcode`` اختیاری است ولی بدون آن اسکن دریافت کار نمی‌کند، پس سایت
    این را هشدار می‌دهد و جلوی ثبت مادهٔ بی‌بارکد را فقط وقتی می‌گیرد که
    کاربر واقعاً بخواهد (مادهٔ مصرفی داخلی).
    """

    class Meta:
        model = RawMaterial
        fields = [
            'category', 'name', 'code', 'barcode', 'unit',
            'min_stock_alert', 'pack_size', 'is_active',
        ]
        widgets = {
            'category': forms.Select(attrs={'class': 'form-select'}),
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'نام ماده اولیه',
            }),
            'code': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'کد (اختیاری)',
            }),
            'barcode': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'بارکد — برای اسکن دریافت کالا',
            }),
            'unit': forms.Select(attrs={'class': 'form-select'}),
            'min_stock_alert': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'min': '0',
            }),
            'pack_size': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01', 'min': '0',
                'placeholder': 'مقدار هر بسته — مثلاً ۴ برای قوطی ۴ لیتری',
            }),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def clean_pack_size(self):
        """
        صفر یعنی «بسته‌بندی ندارد» و مجاز است، ولی مقدار منفی معنی ندارد.
        """
        pack_size = self.cleaned_data.get('pack_size')
        if pack_size is not None and pack_size < 0:
            raise ValidationError('اندازهٔ بسته نمی‌تواند منفی باشد.')
        return pack_size

    def clean_barcode(self):
        """
        بارکد در کل سیستم یکتا است؛ اگر مادهٔ دیگری آن را دارد خطا می‌دهیم
        تا اسکن، مادهٔ اشتباه را وارد نکند.
        """
        barcode = (self.cleaned_data.get('barcode') or '').strip()
        if not barcode:
            return None
        clash = RawMaterial.objects.filter(barcode=barcode)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            other = clash.first()
            raise ValidationError(
                f'این بارکد قبلاً برای «{other.name}» ثبت شده است.'
            )
        return barcode