# fields.py
import jdatetime
from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone


class PersianDateField(models.DateField):
    """
    فیلد تاریخ شمسی که در دیتابیس به صورت میلادی ذخیره می‌شود
    و در سطح پایتون به صورت jdatetime.date ارائه می‌شود.
    """
    description = "Persian date field (stores as Gregorian)"

    def formfield(self, **kwargs):
        from django import forms

        class PersianFormDateField(forms.CharField):
            def to_python(self, value):
                if not value:
                    return None
                if isinstance(value, jdatetime.date):
                    return value
                try:
                    y, m, d = map(int, str(value).strip().replace('/', '-').split('-'))
                    return jdatetime.date(y, m, d)
                except (ValueError, TypeError):
                    raise forms.ValidationError('فرمت تاریخ نامعتبر است (مثال: ۱۴۰۴-۰۱-۰۱)')

            def prepare_value(self, value):
                if isinstance(value, jdatetime.date):
                    return value.strftime('%Y-%m-%d')
                return value

        defaults = {'form_class': PersianFormDateField}
        defaults.update(kwargs)
        return super().formfield(**defaults)

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
            # تلاش برای تبدیل رشته به تاریخ شمسی
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
            # ذخیره به صورت میلادی در دیتابیس
            return value.togregorian()
        return super().get_prep_value(value)

    def value_to_string(self, obj):
        val = self.value_from_object(obj)
        if val:
            return val.strftime('%Y-%m-%d')
        return ''