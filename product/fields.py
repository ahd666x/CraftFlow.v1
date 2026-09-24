# fields.py
import re
import jdatetime
from django.db import models
from django import forms
from django.core.exceptions import ValidationError


class PersianDateFormField(forms.DateField):
    """A forms.DateField that accepts and displays Persian (Jalali) dates."""

    def prepare_value(self, value):
        if isinstance(value, jdatetime.date):
            return value.strftime('%Y-%m-%d')
        return value

    def to_python(self, value):
        if value in self.empty_values:
            return None
        if isinstance(value, jdatetime.date):
            return value
        text = str(value).strip()
        # Convert Persian/Arabic digits to Latin
        text = text.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
        text = text.replace('/', '-')
        match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
        if not match:
            raise ValidationError(self.error_messages['invalid'], code='invalid')
        y, m, d = (int(g) for g in match.groups())
        try:
            return jdatetime.date(y, m, d)
        except ValueError:
            raise ValidationError(self.error_messages['invalid'], code='invalid')


class PersianDateField(models.DateField):
    """
    فیeld تاریخ شمسی که در دیتابیس به صورت میلادی ذخیره می‌شود
    و در سطح پایتون به صورت jdatetime.date ارائه می‌شود.
    """
    description = "Persian date field (stores as Gregorian)"

    def formfield(self, **kwargs):
        widget = kwargs.get('widget')
        if widget is None or (isinstance(widget, type) and issubclass(widget, forms.DateInput)) or (
            isinstance(widget, forms.DateInput) and not isinstance(widget, type)
        ):
            kwargs['widget'] = forms.TextInput(
                attrs={'placeholder': 'YYYY-MM-DD', 'dir': 'ltr', 'autocomplete': 'off'}
            )
        return super().formfield(**{'form_class': PersianDateFormField, **kwargs})

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        return jdatetime.date.fromgregorian(date=value)

    def to_python(self, value):
        if value is None:
            return None
        if isinstance(value, jdatetime.date):
            return value
        if isinstance(value, str):
            try:
                year, month, day = map(int, value.split('-'))
                return jdatetime.date(year, month, day)
            except (ValueError, TypeError):
                pass
        return super().to_python(value)

    def get_prep_value(self, value):
        if value is None:
            return None
        if isinstance(value, jdatetime.date):
            return value.togregorian()
        return super().get_prep_value(value)

    def value_to_string(self, obj):
        val = self.value_from_object(obj)
        if val:
            return val.strftime('%Y-%m-%d')
        return ''