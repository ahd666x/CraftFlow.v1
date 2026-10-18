"""
پر کردن اسلاگ رکوردهای محصول و دسته که پیش از این مهاجرت وجود داشتند.

از مدل تاریخی (`apps.get_model`) استفاده می‌کنیم، نه از مدل زنده‌ی
`product.models`، تا این مهاجرت به کد امروزی وابسته نباشد.

منطق یکتایی عمداً همان چیزی است که `product.models.unique_unicode_slug`
انجام می‌دهد؛ اینجا نمی‌توانیم آن را صدا بزنیم چون مدل‌های تاریخی
`unique` ندارند و ایندکس یکتا هنوز ساخته نشده.
"""
from django.db import migrations
from django.utils.text import slugify


def backfill_slugs(apps, schema_editor):
    def make_slug(text, model, pk, max_length):
        base = slugify(text, allow_unicode=True)[:max_length] or 'item'
        slug = base
        counter = 1
        while model.objects.filter(slug=slug).exclude(pk=pk).exists():
            counter += 1
            suffix = f'-{counter}'
            slug = f'{base[:max_length - len(suffix)]}{suffix}'
        return slug

    Product = apps.get_model('product', 'Product')
    ProductCategory = apps.get_model('product', 'ProductCategory')

    for model, max_length in ((Product, 220), (ProductCategory, 120)):
        for obj in model.objects.all().iterator():
            if obj.slug:
                continue
            obj.slug = make_slug(obj.name, model, obj.pk, max_length)
            obj.save(update_fields=['slug'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0021_product_slug_no_unique'),
    ]

    operations = [
        migrations.RunPython(backfill_slugs, noop),
    ]
