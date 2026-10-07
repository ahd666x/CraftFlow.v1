from django import forms

from .models import Discount


class DiscountForm(forms.ModelForm):
    class Meta:
        model = Discount
        fields = ['code', 'discount_type', 'value', 'max_uses', 'valid_from', 'valid_until', 'is_active']
        widgets = {
            'valid_from': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'valid_until': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }


class ApplyDiscountForm(forms.Form):
    code = forms.CharField(max_length=50, label="کد تخفیف")