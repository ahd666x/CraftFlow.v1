# forms.py
from django import forms
from .models import Order, OrderItem, Color, ProductCategory, PaintingProcess, PaintingStage, PaintingMaterialRequirement, WorkerProfile, Customer
from django.contrib.auth.models import User

import ast

# forms.py (افزودن به انتهای فایل)

class OrderEditForm(forms.ModelForm):
    """فرم ویرایش سفارش برای ادمین/مدیران"""
    class Meta:
        model = Order
        fields = ['customer', 'number', 'due_date', 'priority', 'status']
        widgets = {
            'customer': forms.Select(attrs={'class': 'form-select'}),
            'number': forms.TextInput(attrs={'class': 'form-control'}),
            'due_date': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'YYYY-MM-DD'}),
            'priority': forms.Select(attrs={'class': 'form-select'}),
            'status': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'customer': 'مشتری',
            'number': 'شماره سفارش',
            'due_date': 'تاریخ تحویل',
            'priority': 'اولویت',
            'status': 'وضعیت',
        }


class ColorSelectionForm(forms.Form):
    PART_CHOICES = Color.PART_CHOICES
    CODE_CHOICES = Color.CODE_CHOICES

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for part_value, part_label in self.PART_CHOICES:
            self.fields[f'color_{part_value}'] = forms.ChoiceField(
                choices=[('', '---------')] + list(self.CODE_CHOICES),
                label=part_label,
                required=False,
                widget=forms.Select(attrs={'class': 'form-select'})
            )



from django import forms
from .models import OrderItem, Product, ProductCategory

class OrderItemForm(forms.ModelForm):

    category = forms.ModelChoiceField(
        queryset=ProductCategory.objects.all(),
        label="دسته بندی",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_category'})
    )
    product = forms.ChoiceField(
        label="محصول",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_product'})
    )

    class Meta:
        model = OrderItem
        fields = ['quantity', 'size', 'notes']
        widgets = {
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'size': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'اختیاری'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'یادداشت'}),
        }
        labels = {
            'quantity': 'تعداد',
            'size': 'اندازه',
            'notes': 'توضیحات',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # اگر نمونه موجود باشد
        if self.instance and self.instance.pk:
            self.fields['category'].initial = self.instance.product.category
            self.fields['product'].choices = [(self.instance.product.id, str(self.instance.product))]
        else:
            self.fields['product'].choices = [('', '---------')]

        # اگر داده‌های POST موجود باشد، choices محصول را بر اساس دسته انتخابی تنظیم کن
        if 'category' in self.data:
            try:
                category_id = int(self.data.get('category'))
                products = Product.objects.filter(category_id=category_id).order_by('name')
                self.fields['product'].choices = [(p.id, str(p)) for p in products]
            except (ValueError, TypeError):
                pass

    def clean_product(self):
        product_id = self.cleaned_data.get('product')
        if not product_id:
            raise forms.ValidationError("لطفاً یک محصول انتخاب کنید.")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise forms.ValidationError("محصول انتخاب‌شده معتبر نیست.")
        return product




class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['name', 'phone', 'address']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }
        labels = {
            'name': 'نام مشتری',
            'phone': 'شماره تماس',
            'address': 'آدرس',
        }






from django import forms
from django.contrib.auth.models import User
from .models import Customer

class OrderCustomerForm(forms.Form):
    # فیلد نماینده (مخصوص ادمین)
    representative = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True),
        label="نماینده",
        required=False,
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_representative'})
    )
    # فیلد مشتری
    customer = forms.ModelChoiceField(
        queryset=Customer.objects.none(),
        label="مشتری",
        required=False,
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_customer'})
    )
    # فیلدهای مشتری جدید
    new_customer_name = forms.CharField(
        max_length=100,
        label="نام مشتری جدید",
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    new_customer_phone = forms.CharField(
        max_length=20,
        label="تلفن مشتری جدید",
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    new_customer_address = forms.CharField(
        label="آدرس مشتری جدید",
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2})
    )
    # فیلد شماره سفارش
    number = forms.CharField(
        max_length=10,
        required=False,
        label="شماره سفارش",
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.is_admin = (user.is_superuser or user.groups.filter(name='مدیران').exists()) if user else False
        if self.is_admin:
            self.fields['representative'].queryset = User.objects.filter(is_active=True)
        else:
            self.fields.pop('representative', None)
        # تنظیم queryset اولیه فیلد مشتری
        if user and not self.is_admin:
            self.fields['customer'].queryset = Customer.objects.filter(user=user)
        else:
            self.fields['customer'].queryset = Customer.objects.none()

    def clean(self):
        cleaned = super().clean()
        customer = cleaned.get('customer')
        new_name = cleaned.get('new_customer_name')
        if not customer and not new_name:
            raise forms.ValidationError("لطفاً یک مشتری انتخاب کنید یا نام مشتری جدید را وارد کنید.")
        return cleaned


from .models import Part
class PartForm(forms.ModelForm):
    routing_code = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control form-control-sm'}))
    class Meta:
        model = Part
        fields = [
            'name', 'material', 'length', 'width', 'grain', 'pname' , 'turn',
            'f26', 'f18', 'f4', 'f5',
            'f3', 'f2', 'routing_code', 'base_part',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'material': forms.Select(attrs={'class': 'form-select form-select-sm'}),
            'length': forms.NumberInput(attrs={'class': 'form-control form-control-sm'}),
            'width': forms.NumberInput(attrs={'class': 'form-control form-control-sm'}),
            'grain': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'product': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'turn': forms.CheckboxInput(attrs={'class': 'form-check-input'}),

            'f26': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'f18': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'f4': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'f5': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'f3': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'f2': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'routing_code': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),
            'base_part': forms.Select(attrs={'class': 'form-select form-select-sm'}),
        }
















# class CustomerInfoForm(forms.ModelForm):
#     # فیلد اضافی برای شماره سفارش (در مدل Order است، نه Customer)
#     number = forms.CharField(
#         max_length=10,
#         required=False,
#         label="شماره سفارش",
#         widget=forms.TextInput(attrs={'class': 'form-control'})
#     )

#     class Meta:
#         model = Customer
#         fields = ['name']
#         widgets = {
#             'name': forms.TextInput(attrs={'class': 'form-control'}),
#         }
#         labels = {
#             'name': 'نام مشتری',
#         }








class CustomerInfoForm(forms.Form):
    name = forms.CharField(max_length=100, label="نام مشتری", widget=forms.TextInput(attrs={'class': 'form-control'}))
    phone = forms.CharField(max_length=20, required=False, label="تلفن", widget=forms.TextInput(attrs={'class': 'form-control'}))
    address = forms.CharField(required=False, label="آدرس", widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}))
    number = forms.CharField(max_length=10, required=False, label="شماره سفارش", widget=forms.TextInput(attrs={'class': 'form-control'}))
























# class EditOrderItemForm(forms.ModelForm):
#     class Meta:
#         model = OrderItem
#         fields = ['quantity', 'size', 'notes']
#         widgets = {
#             'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
#             'size': forms.TextInput(attrs={'class': 'form-control'}),
#             'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
#         }
#         labels = {
#             'quantity': 'تعداد',
#             'size': 'اندازه',
#             'notes': 'توضیحات',
#         }

class EditOrderItemForm(forms.ModelForm):
    category = forms.ModelChoiceField(
        queryset=ProductCategory.objects.all(),
        label="دسته بندی",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_category'})
    )
    product = forms.ChoiceField(
        label="محصول",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_product'})
    )

    class Meta:
        model = OrderItem
        fields = ['quantity', 'size', 'notes']
        widgets = {
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'size': forms.TextInput(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }
        labels = {
            'quantity': 'تعداد',
            'size': 'اندازه',
            'notes': 'توضیحات',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            # دسته و محصول فعلی
            current_category = self.instance.product.category
            current_product_id = self.instance.product.id

            self.fields['category'].initial = current_category
            # همهٔ محصولات این دسته را در لیست کشویی قرار بده
            products = Product.objects.filter(category=current_category).order_by('name')
            self.fields['product'].choices = [(p.id, str(p)) for p in products]
            self.fields['product'].initial = current_product_id   # ← این خط کلیدی است
        else:
            self.fields['product'].choices = [('', '---------')]

        # اگر داده‌های POST ارسال شده باشند، محصولات را بر اساس دستهٔ انتخابی به‌روز کن
        if 'category' in self.data:
            try:
                category_id = int(self.data.get('category'))
                products = Product.objects.filter(category_id=category_id).order_by('name')
                self.fields['product'].choices = [(p.id, str(p)) for p in products]
            except (ValueError, TypeError):
                pass

    def clean_product(self):
        product_id = self.cleaned_data.get('product')
        if not product_id:
            raise forms.ValidationError("لطفاً یک محصول انتخاب کنید.")
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            raise forms.ValidationError("محصول انتخاب‌شده معتبر نیست.")
        return product


















import ast
from django import forms
from .models import Product, Part, Color


class ProductCreateForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ['category', 'name', 'base_price', 'default_size']
        widgets = {
            'category': forms.Select(attrs={'class': 'form-select', 'id': 'id_category'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'id': 'id_name'}),
            'base_price': forms.NumberInput(attrs={'class': 'form-control'}),
            'default_size': forms.TextInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'category': 'دسته‌بندی',
            'name': 'نام محصول',
            'base_price': 'قیمت پایه (ریال)',
            'default_size': 'سایز پیش‌فرض',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        raw_colors = self.instance.default_colors if self.instance and self.instance.pk else {}
        if isinstance(raw_colors, str):
            try:
                colors = ast.literal_eval(raw_colors)
                if not isinstance(colors, dict):
                    colors = {}
            except Exception:
                colors = {}
        elif isinstance(raw_colors, dict):
            colors = raw_colors
        else:
            colors = {}

        for part, label in Color.PART_CHOICES:
            field_name = f'color_{part}'
            initial = colors.get(part, '')
            self.fields[field_name] = forms.ChoiceField(
                label=label,
                required=False,
                choices=[('', '---------')] + Color.CODE_CHOICES,
                initial=initial,
                widget=forms.Select(attrs={'class': 'form-select'})
            )


class PartForm(forms.ModelForm):
    """فرم ایجاد / ویرایش قطعه (مودال)"""
    class Meta:
        model = Part
        fields = [
            'name', 'material', 'length', 'width', 'grain', 'pname', 'turn',
            'f26', 'f18', 'f4', 'f5', 'f3', 'routing_code', 'base_part',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'material': forms.Select(attrs={'class': 'form-select'}),
            'length': forms.NumberInput(attrs={'class': 'form-control'}),
            'width': forms.NumberInput(attrs={'class': 'form-control'}),
            'grain': forms.HiddenInput(),
            'pname': forms.HiddenInput(),
            'turn': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'f26': forms.TextInput(attrs={'class': 'form-control'}),
            'f18': forms.TextInput(attrs={'class': 'form-control'}),
            'f4': forms.TextInput(attrs={'class': 'form-control'}),
            'f5': forms.TextInput(attrs={'class': 'form-control'}),
            'f3': forms.TextInput(attrs={'class': 'form-control'}),
            'routing_code': forms.TextInput(attrs={'class': 'form-control'}),
            'base_part': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'name': 'نام قطعه',
            'material': 'متریال',
            'length': 'طول (X)',
            'width': 'عرض (Y)',
            'f26': 'F26',
            'f18': 'F18',
            'f4': 'F4',
            'f5': 'F5',
            'f3': 'F3 (بارکد)',
            'routing_code': 'مسیر تولید',
            'base_part': 'قطعه پایه',
        }



# ===================== فرم‌های مدیریت نقاشی =====================

class PaintingProcessForm(forms.ModelForm):
    class Meta:
        model = PaintingProcess
        fields = ['name', 'code', 'color_codes', 'is_active', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'code': forms.TextInput(attrs={'class': 'form-control'}),
            'color_codes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': '["8","9","10","11"]'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }
        help_texts = {
            'color_codes': 'لیست کدهای رنگی را به فرمت JSON وارد کنید. مثال: ["8","9","10","11"]',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['color_codes'].required = False
        self.fields['description'].required = False

    def clean_color_codes(self):
        import json
        value = self.cleaned_data.get('color_codes')
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except (json.JSONDecodeError, TypeError):
            raise forms.ValidationError('فرمت JSON نامعتبر است. مثال: ["8","9"]')

    def clean(self):
        cleaned = super().clean()
        is_active = cleaned.get('is_active', True)
        codes = cleaned.get('color_codes') or []
        if is_active and codes:
            others = PaintingProcess.objects.filter(is_active=True)
            if self.instance and self.instance.pk:
                others = others.exclude(pk=self.instance.pk)
            for other in others:
                other_codes = set(str(c) for c in (other.color_codes or []))
                overlap = other_codes.intersection(str(c) for c in codes)
                if overlap:
                    self.add_error(
                        'color_codes',
                        f'کد(های) رنگی {", ".join(overlap)} در روند فعال دیگری ("{other.name}") قبلاً استفاده شده‌اند.'
                    )
        return cleaned


class PaintingStageForm(forms.ModelForm):
    class Meta:
        model = PaintingStage
        fields = ['process', 'order', 'name', 'duration_minutes', 'drying_time_minutes', 'required_skill']
        widgets = {
            'process': forms.Select(attrs={'class': 'form-select'}),
            'order': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'duration_minutes': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'drying_time_minutes': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'required_skill': forms.Select(attrs={'class': 'form-select'}),
        }


class PaintingMaterialRequirementForm(forms.ModelForm):
    class Meta:
        model = PaintingMaterialRequirement
        fields = ['process', 'product', 'color_part', 'raw_material', 'consumption_per_unit']
        widgets = {
            'process': forms.Select(attrs={'class': 'form-select'}),
            'raw_material': forms.Select(attrs={'class': 'form-select select2-raw-material'}),
            'product': forms.Select(attrs={'class': 'form-select'}),
            'color_part': forms.Select(attrs={'class': 'form-select'}),
            'consumption_per_unit': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': '0.001'}),
        }
        labels = {
            'process': 'روند نقاشی',
            'product': 'محصول',
            'color_part': 'بخش رنگی',
            'raw_material': 'ماده اولیه',
            'consumption_per_unit': 'مقدار مصرف',
        }


class WorkerProfileForm(forms.ModelForm):
    class Meta:
        model = WorkerProfile
        fields = ['user', 'stage', 'skills', 'skill_priority', 'is_available', 'excluded_products']
        widgets = {
            'user': forms.Select(attrs={'class': 'form-select'}),
            'stage': forms.Select(attrs={'class': 'form-select'}),
            'skills': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': '["painter","sealer"]'}),
            'skill_priority': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': '{"painter":3, "sealer":4}'}),
            'is_available': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'excluded_products': forms.CheckboxSelectMultiple(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['skills'].required = False
        self.fields['skill_priority'].required = False

    def clean_skills(self):
        import json
        value = self.cleaned_data.get('skills')
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except (json.JSONDecodeError, TypeError):
            raise forms.ValidationError('فرمت JSON نامعتبر است. مثال: ["painter","sealer"]')

    def clean_skill_priority(self):
        import json
        value = self.cleaned_data.get('skill_priority')
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            raise forms.ValidationError('فرمت JSON نامعتبر است. مثال: {"painter":3}')















