from django import forms
from django.core.exceptions import ValidationError

from .models import Discount


class DiscountForm(forms.ModelForm):
    class Meta:
        model = Discount
        fields = ['code', 'discount_type', 'value', 'max_uses', 'valid_from', 'valid_until', 'is_active']
        widgets = {
            'valid_from': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'valid_until': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Ensure datetime-local format works correctly
        for field_name in ['valid_from', 'valid_until']:
            if field_name in self.fields:
                self.fields[field_name].input_formats = ['%Y-%m-%dT%H:%M']

    def clean(self):
        cleaned_data = super().clean()
        discount_type = cleaned_data.get('discount_type')
        value = cleaned_data.get('value')
        valid_from = cleaned_data.get('valid_from')
        valid_until = cleaned_data.get('valid_until')

        # Validate percent discount value
        if discount_type == 'percent' and value is not None:
            if value < 0 or value > 100:
                raise ValidationError({'value': 'درصد تخفیف باید بین ۰ تا ۱۰۰ باشد.'})

        # Validate date range
        if valid_from and valid_until:
            if valid_until <= valid_from:
                raise ValidationError({'valid_until': 'تاریخ انقضا باید بعد از تاریخ شروع باشد.'})

        return cleaned_data


class ApplyDiscountForm(forms.Form):
    code = forms.CharField(max_length=50, label="کد تخفیف")