"""ادمین دستورالعمل‌های تولید.

- ثبت دستورالعمل با اینلاین عکس و فیلترهای ایستگاه/نوع/فعال.
- اینلاین دستورالعمل‌ها داخل صفحه‌ی ویرایش محصول تا مدیر در همان‌جا بنویسد.
- لینک سند «دستور تولید» محصول از داخل ادمین.
"""
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from product.models import Material, Product

from .models import WorkInstruction, WorkInstructionImage


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    """ادمین سبک متریال (قبلاً ثبت نشده بود) — برای انتخاب شرط متریال و مرور فهرست."""

    list_display = ('name', 'thickness')
    search_fields = ('name',)
    list_filter = ('thickness',)


class WorkInstructionImageInline(admin.TabularInline):
    model = WorkInstructionImage
    extra = 0
    fields = ('sort_order', 'image', 'caption')
    autocomplete_fields = ()
    verbose_name = 'تصویر'
    verbose_name_plural = 'تصاویر دستورالعمل'


@admin.register(WorkInstruction)
class WorkInstructionAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'target_display', 'station_display', 'kind', 'conditions_display',
        'sequence', 'active', 'images_count', 'updated_at',
    )
    list_filter = ('station', 'kind', 'active', 'condition_material')
    list_editable = ('sequence', 'active')
    search_fields = ('title', 'body', 'product__name', 'part__name')
    autocomplete_fields = ('product', 'part', 'condition_material', 'condition_color')
    inlines = [WorkInstructionImageInline]
    list_per_page = 50
    save_on_top = True

    fieldsets = (
        ('مقصد و ایستگاه', {
            'fields': ('product', 'part', 'station', 'kind', 'sequence'),
        }),
        ('محتوا', {
            'fields': ('title', 'body'),
        }),
        ('شرط نمایش (اختیاری)', {
            'classes': ('collapse',),
            'fields': ('condition_material', 'condition_color'),
            'description': 'خالی = همیشه نمایش. شرط متریال و رنگ با هم AND می‌شوند.',
        }),
        ('وضعیت', {
            'fields': ('active',),
        }),
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description='مقصد')
    def target_display(self, obj):
        if obj.product_id:
            return obj.product.name
        if obj.part_id:
            return f"قطعه: {obj.part.name}"
        return '—'

    @admin.display(description='ایستگاه', ordering='station')
    def station_display(self, obj):
        return obj.get_station_display()

    @admin.display(description='شرط نمایش')
    def conditions_display(self, obj):
        labels = obj.condition_labels
        return ' / '.join(labels) if labels else 'همیشه'

    @admin.display(description='تصاویر')
    def images_count(self, obj):
        return obj.images.count()


class WorkInstructionInline(admin.TabularInline):
    """ویرایش سریع دستورالعمل‌ها از داخل صفحه‌ی محصول."""

    model = WorkInstruction
    extra = 0
    fields = ('station', 'kind', 'title', 'condition_material', 'condition_color',
              'sequence', 'active', 'doc_link')
    readonly_fields = ('doc_link',)
    autocomplete_fields = ('condition_material', 'condition_color')
    verbose_name = 'دستورالعمل'
    verbose_name_plural = 'دستورالعمل‌های این محصول (ویرایش کامل با کلیک روی ردیف)'

    def doc_link(self, obj):
        if not obj.pk:
            return '—'
        url = reverse('instructions:product_doc', args=[obj.product_id])
        return format_html('<a href="{}" target="_blank">سند دستور تولید</a>', url)
    doc_link.short_description = 'سند'

    def get_queryset(self, request):
        # فقط دستورالعمل‌های سطح محصول در اینلاین محصول؛ دانش قطعه‌محور
        # از ادمین مستقل دستورالعمل‌ها مدیریت می‌شود.
        return super().get_queryset(request).filter(product__isnull=False, part__isnull=True)


def register_product_inline():
    """افزودن اینلاین به ادمین موجود محصول.

    در ``apps.ready`` صدا زده می‌شود تا بعد از autodiscovery ادمین باشد و
    ProductAdmin حتماً ثبت شده باشد (بدون بازتعریف کلاس آن).
    """
    from django.contrib import admin as dj_admin

    model_admin = dj_admin.site._registry.get(Product)
    if model_admin is not None and WorkInstructionInline not in model_admin.inlines:
        model_admin.inlines = list(model_admin.inlines) + [WorkInstructionInline]
