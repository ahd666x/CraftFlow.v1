class UnicodeSlugConverter:
    """
    مبدل مسیر برای اسلاگ‌هایی که ممکن است شامل حروف فارسی/یونیکد باشند.
    اسلاگ پیش‌فرض جنگو (`slug`) فقط حروف/اعداد لاتین را قبول می‌کند،
    ولی نام محصولات و دسته‌ها در این پروژه فارسی هستند.
    """
    regex = r'[-\w]+'

    def to_python(self, value):
        return value

    def to_url(self, value):
        return str(value)
