from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory
from .models import Supplier, RawMaterialCategory, RawMaterial, StockMovement, PurchaseOrder, PurchaseOrderItem


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ['name', 'phone', 'address', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'نام تامین‌کننده'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'تلفن'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'آدرس'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class RawMaterialCategoryForm(forms.ModelForm):
    class Meta:
        model = RawMaterialCategory
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'نام دسته (مثال: رنگ، آستر، یراق‌آلات)'}),
        }


class RawMaterialForm(forms.ModelForm):
    class Meta:
        model = RawMaterial
        fields = ['category', 'name', 'code', 'barcode', 'unit', 'min_stock_alert', 'pack_size', 'is_active']
        widgets = {
            'category': forms.Select(attrs={'class': 'form-select'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'نام ماده اولیه'}),
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'کد (اختیاری)'}),
            'barcode': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'بارکد (اختیاری، برای اسکن دریافت)'}),
            'unit': forms.Select(attrs={'class': 'form-select'}),
            'min_stock_alert': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0'}),
            'pack_size': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'placeholder': 'مثلاً 4 برای قوطی ۴ لیتری'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class StockMovementForm(forms.ModelForm):
    class Meta:
        model = StockMovement
        fields = ['raw_material', 'movement_type', 'quantity', 'unit_price', 'supplier', 'note']
        widgets = {
            'raw_material': forms.Select(attrs={'class': 'form-select'}),
            'movement_type': forms.Select(attrs={'class': 'form-select'}),
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0.01'}),
            'unit_price': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
            'supplier': forms.Select(attrs={'class': 'form-select'}),
            'note': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'یادداشت (اختیاری)'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['supplier'].required = False
        self.fields['unit_price'].required = False

    def clean(self):
        cleaned_data = super().clean()
        movement_type = cleaned_data.get('movement_type')
        quantity = cleaned_data.get('quantity')
        supplier = cleaned_data.get('supplier')
        raw_material = cleaned_data.get('raw_material')

        if quantity is not None and quantity <= 0:
            raise ValidationError('مقدار باید بزرگتر از صفر باشد.')

        if movement_type == 'purchase' and not supplier:
            raise ValidationError('برای خرید/ورود باید تامین‌کننده انتخاب شود.')

        if movement_type == 'consumption' and raw_material:
            current = raw_material.current_stock
            if current < quantity:
                raise ValidationError(f'موجودی کافی نیست. موجودی فعلی: {current}')

        return cleaned_data


class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = ['supplier', 'status', 'note']
        widgets = {
            'supplier': forms.Select(attrs={'class': 'form-select'}),
            'status': forms.Select(attrs={'class': 'form-select'}),
            'note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'یادداشت'}),
        }


class PurchaseOrderItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrderItem
        fields = ['raw_material', 'quantity', 'unit_price']
        widgets = {
            'raw_material': forms.Select(attrs={'class': 'form-select'}),
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0.01'}),
            'unit_price': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
        }


PurchaseOrderItemFormSet = inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderItem,
    form=PurchaseOrderItemForm,
    extra=1,
    can_delete=True
)