from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.contrib.auth.models import User
from django.urls import reverse
import jdatetime
from .fields import PersianDateField
from io import BytesIO
from django.core.files.base import ContentFile
from django.conf import settings
import qrcode
from decimal import Decimal
from datetime import time



class Customer(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customers',
        verbose_name="نماینده"
    )
    name = models.CharField(max_length=100, verbose_name="نام مشتری")
    phone = models.CharField(max_length=20, blank=True, verbose_name="تلفن")
    address = models.TextField(blank=True, verbose_name="آدرس")

    def __str__(self):
        return f"{self.name}"

    class Meta:
        verbose_name = "مشتری"
        verbose_name_plural = "مشتریان"


class ProductCategory(models.Model):
    name = models.CharField(max_length=100, verbose_name="نام دسته")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "دسته بندی"
        verbose_name_plural = "دسته بندی‌ها"


class Material(models.Model):
    name = models.CharField(max_length=100, verbose_name="نام ورق")
    thickness = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        verbose_name="ضخامت (میلی‌متر)",
        help_text="مثال: 16.0"
    )
    raw_material = models.ForeignKey(
        'inventory.RawMaterial',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='product_materials',
        verbose_name="ماده اولیه انبار (برای مصرف خودکار)"
    )
    consumption_per_unit = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=1,
        verbose_name="مقدار مصرف به ازای هر قطعه",
        help_text="مقدار مصرف به ازای هر قطعه"
    )

    def __str__(self):
        return f"{self.name} ({self.thickness}mm)"

    class Meta:
        verbose_name = "متریال"
        verbose_name_plural = "متریال‌ها"



class Order(models.Model):
    ORDER_STATUS = (
        ('draft', 'پیش‌نویس'),
        ('planned', 'برنامه‌ریزی شده'),
        ('producing', 'در حال تولید'),
        ('completed', 'تکمیل شده'),
    )

    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, verbose_name="نماینده")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, verbose_name="مشتری")
    number = models.CharField(max_length=10, blank=True, verbose_name="شماره سفارش")
    created_at = PersianDateField(default=jdatetime.date.today, verbose_name="تاریخ سفارش")
    due_date = PersianDateField(null=True, blank=True, verbose_name="تاریخ تحویل")
    priority = models.PositiveSmallIntegerField(
        default=3,
        choices=[(1, 'بسیار بالا'), (2, 'بالا'), (3, 'متوسط'), (4, 'پایین')],
        verbose_name="اولویت"
    )
    status = models.CharField(
        max_length=20,
        choices=ORDER_STATUS,
        default='draft',
        verbose_name="وضعیت سفارش"
    )

    def __str__(self):
        return f"سفارش {self.id} - {self.customer}"

    class Meta:
        verbose_name = "سفارش"
        verbose_name_plural = "سفارش‌ها"
    @property
    def total_price(self):
        return sum(item.line_total for item in self.items.all())
    

    @property
    def packaging_summary(self):
        total_units = 0
        packed_units = 0
        shipped_units = 0
        for item in self.items.all():
            item_total = item.packaging_units.count()
            total_units += item_total
            packed_units += item.packaging_units.filter(is_packed=True).count()
            shipped_units += item.packaging_units.filter(is_shipped=True).count()
        return {
            'total': total_units,
            'packed': packed_units,
            'shipped': shipped_units,
        }



    def generate_tasks(self):
        from .utils import (
            get_material_for_color,
            parse_size_string,
            apply_size_adjustment,
            update_barcode_size,
        )
        from .models import ProductionTask

        tasks_to_create = []
        has_bom = False
        with transaction.atomic():
            # Phase 1: regular station tasks per BOM part (global sequential step_order)
            current_step = 0

            for item in self.items.all():
                item_colors = {c.part: c.code for c in item.ordercolor.all()}
                order_item_id = item.id

                product_default_size = parse_size_string(item.product.default_size or "")
                ordered_size = parse_size_string(item.size or "")
                size_diff = {}
                if product_default_size.get('length') and ordered_size.get('length'):
                    diff_cm = ordered_size['length'] - product_default_size['length']
                    size_diff['length_diff'] = diff_cm * 10
                if product_default_size.get('width') and ordered_size.get('width'):
                    diff_cm = ordered_size['width'] - product_default_size['width']
                    size_diff['width_diff'] = diff_cm * 10

                for bom_entry in item.product.bom.all():
                    has_bom = True
                    part = bom_entry.part
                    total_qty = bom_entry.quantity * item.quantity

                    material = part.material
                    if bom_entry.allow_material_override and bom_entry.color_part:
                        color_code = item_colors.get(bom_entry.color_part)
                        if color_code:
                            new_material = get_material_for_color(color_code, bom_entry.color_material_map)
                            if new_material:
                                material = new_material

                    length = part.length
                    width = part.width
                    if bom_entry.size_affected and bom_entry.size_adjustment_rule and size_diff:
                        length, width = apply_size_adjustment(
                            part.length, part.width, size_diff, bom_entry.size_adjustment_rule
                        )

                    new_f3 = update_barcode_size(part.f3, length, width, order_item_id)

                    try:
                        dynamic_part = Part.objects.filter(
                            base_part=part,
                            material=material,
                            length=length,
                            width=width
                        ).first()
                        if dynamic_part:
                            created = False
                        else:
                            dynamic_part = Part.objects.create(
                                base_part=part,
                                material=material,
                                length=length,
                                width=width,
                                name=part.name,
                                grain=part.grain,
                                pname=part.pname,
                                turn=part.turn,
                                f26=part.f26,
                                f18=part.f18,
                                f4=part.f4,
                                f5=part.f5,
                                f3=new_f3,
                                f2=part.f2,
                                routing_code=part.routing_code,
                            )
                            created = True
                    except Part.MultipleObjectsReturned:
                        dynamic_part = Part.objects.filter(
                            base_part=part,
                            material=material,
                            length=length,
                            width=width
                        ).first()
                        created = False

                    if not created and dynamic_part.f3 != new_f3:
                        dynamic_part.f3 = new_f3
                        dynamic_part.save(update_fields=['f3'])

                    stations = [s.strip() for s in dynamic_part.routing_code.split('.') if s.strip()]
                    stations.insert(0, "cut")
                    for idx, station_name in enumerate(stations):
                        current_step += 1
                        tasks_to_create.append(
                            ProductionTask(
                                order=self,
                                part=dynamic_part,
                                order_item=item,
                                station_name=station_name.lower(),
                                step_order=current_step,
                                quantity=total_qty,
                                status='pending' if idx == 0 else 'waiting'
                            )
                        )

            # تسک‌های نقاشی فقط از طریق assign_painting_process / painting_assign_process در views.py
            # وقتی آیتم آماده‌ی نقاشی شد، ساخته می‌شوند؛ نه در این لحظه‌ی تولید سفارش.

            if tasks_to_create:
                ProductionTask.objects.bulk_create(tasks_to_create)
                self.status = 'planned'
                self.save()
                return {'success': True}
            elif not has_bom:
                return {'success': False, 'error': 'محصولات این سفارش فرمول ساخت (BOM) ندارند. ابتدا در ویرایش محصول قطعات را اضافه کنید.'}
            else:
                return {'success': False, 'error': 'تسک‌ها قبلاً ایجاد شده‌اند.'}



class Product(models.Model):
    category = models.ForeignKey(ProductCategory,on_delete=models.PROTECT,related_name='products',verbose_name="دسته")
    name = models.CharField(max_length=200, verbose_name="نام محصول")
    color = models.CharField(max_length=100, blank=True, verbose_name="رنگ")
    parts_list_key = models.CharField(max_length=255, blank=True, verbose_name="مسیر فایل")
    description = models.TextField(blank=True, verbose_name="توضیحات")
    default_size = models.CharField(max_length=100, blank=True, verbose_name="سایز پیش‌فرض")
    default_colors = models.JSONField(default=dict,blank=True,verbose_name="رنگ‌های پیش‌فرض")   ####default="{}"
    base_price = models.DecimalField(max_digits=12, decimal_places=0, default=0,verbose_name="قیمت")
    price_increment_per_cm = models.DecimalField(max_digits=5,decimal_places=2,default=0,verbose_name="درصد افزایش قیمت به ازای هر سانتی‌متر",)
    image = models.ImageField(upload_to='product_images/', blank=True, null=True, verbose_name="عکس محصول")

    def __str__(self):
        return f"{self.category} - {self.name}"

    class Meta:
        verbose_name = "محصول"
        verbose_name_plural = "محصولات"



class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items', verbose_name="سفارش")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="محصول")
    notes = models.CharField(max_length=200, blank=True, verbose_name="توضیحات")
    quantity = models.PositiveIntegerField(default=1, verbose_name="تعداد")
    size = models.CharField(max_length=100, blank=True, verbose_name="اندازه")
    qr_code = models.ImageField(upload_to='qr/', blank=True, null=True)
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=0, default=0,verbose_name="قیمت")
    class Meta:
        verbose_name = "لیست سفارش"
        verbose_name_plural = "لیست سفارش‌ها"

    def get_absolute_url(self):
        return reverse('scan_qr', args=[self.id])

    def __str__(self):
        return f"{self.product.name} (سفارش {self.order.id})"

    @property
    def color_summary(self):
        colors = self.ordercolor.all()
        parts = []
        for c in colors:
            if c.code and c.code != 'nan':
                parts.append(f"{c.part}:{c.code}")
        return "-".join(parts) if parts else "بدون رنگ"

    @property
    def packaging_progress(self):
        total = self.packaging_units.count()
        packed = self.packaging_units.filter(is_packed=True).count()
        return packed, total

    @property
    def shipping_progress(self):
        total = self.packaging_units.count()
        shipped = self.packaging_units.filter(is_shipped=True).count()
        return shipped, total

    @property
    def is_fully_packed(self):
        packed, total = self.packaging_progress
        return total > 0 and packed == total

    @property
    def is_fully_shipped(self):
        shipped, total = self.shipping_progress
        return total > 0 and shipped == total

    @property
    def is_painting_complete(self):
        """آیا تمام تسک‌های نقاشی این آیتم به done رسیده‌اند؟"""
        if not hasattr(self, 'paint_tasks'):
            return False
        paint_tasks = self.paint_tasks.all()
        if not paint_tasks.exists():
            return True
        return paint_tasks.filter(status='done').count() == paint_tasks.count()

    @property
    def is_ready_for_delivery(self):
        """آیا این آیتم برای تحویل آماده است؟ (نقاشی کامل + بسته‌بندی کامل)"""
        return self.is_painting_complete and self.is_fully_packed

    @property
    def line_total(self):
        return self.unit_price * self.quantity


    def sync_packaging_units(self):
        """تعداد واحدهای بسته‌بندی را با مقدار quantity هماهنگ می‌کند."""
        current_count = self.packaging_units.count()
        target_count = self.quantity

        if target_count > current_count:
            # ایجاد واحدهای جدید
            base_url = getattr(settings, 'SCAN_BASE_URL', 'https://selvichoob.ir')
            for i in range(current_count + 1, target_count + 1):
                unit = PackagingUnit.objects.create(
                    order_item=self,
                    unit_number=i
                )
                # تولید QR برای واحد جدید
                scan_url = reverse('scan_packaging_unit', args=[unit.id])
                full_url = f"{base_url.rstrip('/')}{scan_url}?next={reverse('item_detail', args=[self.id])}"
                qr = qrcode.make(full_url, box_size=10, border=4)
                buffer = BytesIO()
                qr.save(buffer, format='PNG')
                filename = f"pack_qr_order_{self.order.id}_item_{self.id}_unit_{i}.png"
                unit.qr_code.save(filename, ContentFile(buffer.getvalue()), save=True)

        elif target_count < current_count:
            extra_ids = list(
                self.packaging_units.order_by('-unit_number')
                .values_list('id', flat=True)[:current_count - target_count]
            )
            PackagingUnit.objects.filter(id__in=extra_ids).delete()



    def calculate_price(self):
        """محاسبه قیمت بر اساس طول سفارش"""
        if not self.product:
            return 0
            
        order_size_str = self.size or self.product.default_size
        default_size_str = self.product.default_size
        
        if not order_size_str or not default_size_str:
            return int(self.product.base_price or 0)
            
        import re
        order_numbers = re.findall(r'\d+', order_size_str)
        default_numbers = re.findall(r'\d+', default_size_str)
        
        if not order_numbers or not default_numbers:
            return int(self.product.base_price or 0)
            
        order_length = int(order_numbers[0])
        default_length = int(default_numbers[0])
        
        if default_length == 0:
            return int(self.product.base_price or 0)
            
        base_price_int = int(self.product.base_price or 0)
        increment_percent_float = float(self.product.price_increment_per_cm or 0)
        
        # diff_percent = ((order_length - default_length) / default_length) * 100
        # price_increase = (base_price_int * diff_percent * increment_percent_float) / 100
        # final_price = base_price_int + price_increase

        diff_percent = ((order_length - default_length) * increment_percent_float )/100
        price_increase = (base_price_int * diff_percent ) 
        final_price = base_price_int + price_increase
        
        if final_price < 0:
            return 0
        return int(round(final_price))
    
    def save(self, *args, **kwargs):
        """ذخیره آیتم با محاسبه خودکار قیمت"""
        # محاسبه قیمت قبل از ذخیره
        if self.product:
            try:
                self.unit_price = self.calculate_price()
            except Exception as e:
                # اگر خطایی رخ داد، از قیمت پایه استفاده کن
                if self.product and self.product.base_price is not None:
                    self.unit_price = int(self.product.base_price)
                else:
                    self.unit_price = 0
        
        super().save(*args, **kwargs)
        






class ColorCode(models.Model):
    """
    تعریف کد رنگ — جدول lookup برای نگاشت کد رنگ به هگز و متریال.
    این جدول در ادمین مدیریت می‌شود و امکان افزودن کدهای جدید (مثلاً ۱۳)
    به‌صورت داینامیک را فراهم می‌آورد.
    """
    code = models.CharField(max_length=20, unique=True, verbose_name="کد رنگ")
    hex_code = models.CharField(max_length=7, blank=True, verbose_name="کد هگز رنگ")
    material_name = models.CharField(max_length=50, blank=True, verbose_name="نام متریال پیش‌فرض")
    description = models.CharField(max_length=100, blank=True, verbose_name="توضیحات")
    is_active = models.BooleanField(default=True, verbose_name="فعال")

    def __str__(self):
        return f"{self.code} ({self.hex_code})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .utils import invalidate_caches
        invalidate_caches()

    class Meta:
        verbose_name = "تعریف کد رنگ"
        verbose_name_plural = "تعریف‌های کد رنگ"


class Color(models.Model):
    PART_CHOICES = [
        ('بدنه', 'بدنه'),
        ('درب', 'درب'),
        ('دستگیره', 'دستگیره'),
        ('پایه', 'پایه'),
        ('صفحه', 'صفحه'),
        ('رینگ', 'رینگ'),
    ]
    CODE_CHOICES = [(str(i), str(i)) for i in range(1, 11)]
    CODE_CHOICES.append(('جناغی' , 'جناغی'))
    CODE_CHOICES.append(('بتنی' , 'بتنی'))

    part = models.CharField(max_length=20, choices=PART_CHOICES, verbose_name="قطعه")
    code = models.CharField(max_length=20, choices=CODE_CHOICES, verbose_name="کد رنگ")
    orderitem = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name='ordercolor', verbose_name="آیتم سفارش")
    hex_code = models.CharField(max_length=7, blank=True, verbose_name="کد هگز رنگ")
    material_name = models.CharField(max_length=50, blank=True, verbose_name="نام متریال پیش‌فرض")

    def __str__(self):
        return f"{self.part}:{self.code}"

    def _resolve_color_code(self):
        try:
            return ColorCode.objects.get(code=str(self.code), is_active=True)
        except ColorCode.DoesNotExist:
            return None

    def save(self, *args, **kwargs):
        if not self.hex_code or not self.material_name:
            cc = self._resolve_color_code()
            if cc:
                if not self.hex_code:
                    self.hex_code = cc.hex_code
                if not self.material_name:
                    self.material_name = cc.material_name
        super().save(*args, **kwargs)
        from .utils import invalidate_caches
        invalidate_caches()

    class Meta:
        verbose_name = "رنگ"
        verbose_name_plural = "رنگ‌ها"




class Part(models.Model):
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name='parts',
        verbose_name="نوع ورق"
    )
    name = models.CharField(max_length=100, verbose_name="نام قطعه")
    length = models.DecimalField(max_digits=7, decimal_places=1, verbose_name="طول (X)")
    width = models.DecimalField(max_digits=7, decimal_places=1, verbose_name="عرض (Y)")
    grain = models.CharField(max_length=100, blank=True , verbose_name="دسته")
    pname = models.CharField(max_length=100, verbose_name="نام محصول")
    turn = models.BooleanField(default=False, blank=True , verbose_name="turn")

    # نوار لبه
    f26 = models.CharField(max_length=100, blank=True, verbose_name="نوار لبه F26")
    f18 = models.CharField(max_length=100, blank=True, verbose_name="نوار لبه F18")
    f4 = models.CharField(max_length=100, blank=True, verbose_name="نوار لبه F4")
    f5 = models.CharField(max_length=100, blank=True, verbose_name="نوار لبه F5")

    # بارکد و نام فنی
    f3 = models.CharField(max_length=100, blank=True, verbose_name="بارکد F3")
    f2 = models.CharField(max_length=100, blank=True, verbose_name="نام قطعه F2")

    # مسیر تولید
    routing_code = models.CharField(max_length=255, verbose_name="تحویل به مرحله")

    # فیلدهای تغییر اندازه
    base_part = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='variations',
        verbose_name="قطعه پایه"
    )

    class Meta:
        verbose_name = "قطعه"
        verbose_name_plural = "قطعات"

    def __str__(self):
        return f"{self.f2 or self.name} ({self.length}x{self.width})"


class ProductBOM(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='bom')
    part = models.ForeignKey(Part, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1, verbose_name="تعداد در هر محصول")

    # نقش رنگی قطعه
    color_part = models.CharField(
        max_length=20,
        choices=Color.PART_CHOICES,
        blank=True,
        null=True,
        verbose_name="بخش رنگی"
    )

    # تغییر متریال با رنگ
    allow_material_override = models.BooleanField(default=False, verbose_name="امکان تغییر متریال با رنگ")
    color_material_map = models.JSONField(default=dict, blank=True, verbose_name="نگاشت رنگ به متریال")

    # تغییر اندازه
    size_affected = models.BooleanField(default=False, verbose_name="تحت تأثیر اندازه")
    size_adjustment_rule = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="قانون تغییر اندازه",
        help_text="مثال: length+length_diff, width/3"
    )

    def __str__(self):
        return f"{self.product.name}: {self.quantity}x {self.part.name}"

    class Meta:
        verbose_name = "فرمول ساخت"
        verbose_name_plural = "فرمول‌های ساخت"


STATION_CHOICES = [
    ('cut', 'برش'),
    ('cnc', 'CNC'),
    ('dr', 'سوراخکاری'),
    ('pvc', 'نوارکاری'),       # ← نوارکاری با نام اختصاری pvc
    ('prs', 'پرس'),             # ← پرس با نام اختصاری prs
    # ('assembly1', 'مونتاژ اول'),
    ('mon', 'مونتاژ اول'),
    ('vacum', 'وکیوم'),

    ('paint', 'نقاشی'),
    ('assembly2', 'مونتاژ نهایی'),
    ('packaging', 'بسته‌بندی'),
    ('shipping', 'ارسال شده'),
]


class ProductionTask(models.Model):
    STATION_CHOICES = STATION_CHOICES
    TASK_STATUS = (
        ('waiting', 'در انتظار مرحله قبل'),
        ('pending', 'آماده انجام'),
        ('done', 'تکمیل شده'),
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='tasks',
        null=True,
        blank=True,
        verbose_name="سفارش",
        help_text="خالی = کارت دلخواه نقاشی (بدون سفارش)",
    )
    part = models.ForeignKey(Part, on_delete=models.PROTECT, verbose_name="قطعه", null=True, blank=True)
    station_name = models.CharField(max_length=50, choices=STATION_CHOICES, verbose_name="ایستگاه کاری")
    step_order = models.PositiveIntegerField(verbose_name="اولویت مرحله")
    quantity = models.PositiveIntegerField(verbose_name="عدد قطعه")
    status = models.CharField(max_length=20, choices=TASK_STATUS, default='waiting', verbose_name="وضعیت")
    scanned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="انجام‌دهنده")
    completed_at = PersianDateField(null=True, blank=True, verbose_name="زمان تکمیل")
    painting_stage = models.ForeignKey(
        'PaintingStage',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name="مرحله نقاشی"
    )
    scheduled_start = models.DateTimeField(null=True, blank=True, verbose_name="زمان شروع برنامه‌ریزی شده")
    scheduled_end = models.DateTimeField(null=True, blank=True, verbose_name="زمان پایان برنامه‌ریزی شده")
    assigned_worker = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_tasks',
        verbose_name="کارگر تخصیص‌یافته"
    )
    order_item = models.ForeignKey(
        'OrderItem',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='paint_tasks',
        verbose_name="آیتم سفارش مرتبط (برای نقاشی)"
    )
    color_part = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="بخش رنگی (بدنه، درب، ...)"
    )
    completed_quantity = models.PositiveIntegerField(default=0, verbose_name="تعداد انجام‌شده")
    custom_title = models.CharField(max_length=200, blank=True, verbose_name="عنوان دلخواه")
    custom_duration_minutes = models.PositiveIntegerField(null=True, blank=True, verbose_name="مدت زمان دلخواه (دقیقه)")
    custom_note = models.CharField(max_length=255, blank=True, verbose_name="یادداشت دلخواه")

    class Meta:
        verbose_name = "وظیفه تولید"
        verbose_name_plural = "وظایف تولید"
        ordering = ['order', 'part', 'step_order']
        indexes = [
            models.Index(fields=['station_name', 'scheduled_start']),
            models.Index(fields=['assigned_worker', 'scheduled_start']),
            models.Index(fields=['order_item', 'station_name', 'status']),
        ]

    def __str__(self):
        target = self.part or self.order_item or self.custom_title or "—"
        order_label = self.order_id if self.order_id else "دلخواه"
        return f"{self.get_station_name_display()} | {target} (سفارش {order_label})"

    def save(self, *args, **kwargs):
        old_status = None
        if self.pk:
            old_status = ProductionTask.objects.filter(pk=self.pk).values_list('status', flat=True).first()

        if self.pk and self.completed_quantity >= self.quantity and self.status != 'done':
            self.status = 'done'

        if self.status == 'done' and old_status != 'done':
            if not self.completed_at:
                self.completed_at = jdatetime.date.today()
            if self.completed_quantity < self.quantity:
                self.completed_quantity = self.quantity
            if kwargs.get('update_fields'):
                uf = list(kwargs['update_fields'])
                if 'completed_quantity' not in uf:
                    uf.append('completed_quantity')
                kwargs['update_fields'] = tuple(uf)

        super().save(*args, **kwargs)

        if self.status == 'done' and old_status != 'done':
            try:
                from .utils import log_production_event
                log_production_event(
                    task=self,
                    event_type='done',
                    user=self.scanned_by,
                    old_status=old_status or '',
                    new_status='done',
                    quantity=self.completed_quantity or self.quantity,
                )
            except Exception:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("خطا در ثبت ProductionEvent (نادیده گرفته شد تا جریان اصلی مختل نشود)")

            try:
                from .utils import consume_material_for_task, consume_material_for_paint_task
                if self.station_name == 'paint':
                    consume_material_for_paint_task(self)
                else:
                    consume_material_for_task(self)
            except Exception:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("خطا در مصرف خودکار مواد اولیه برای تسک %s", self.pk)

            next_step = None
            if self.order_id is None:
                pass
            elif self.station_name == 'paint' and self.order_item_id:
                next_step = ProductionTask.objects.filter(
                    order=self.order,
                    station_name='paint',
                    order_item=self.order_item,
                    color_part=self.color_part,
                    step_order=self.step_order + 1,
                ).first()
            else:
                next_step = ProductionTask.objects.filter(
                    order=self.order,
                    part=self.part,
                    step_order=self.step_order + 1,
                ).first()

            if next_step and next_step.status == 'waiting':
                next_step.status = 'pending'
                next_step.save()

            self.update_order_status()

    def update_order_status(self):
        if self.order_id is None:
            return
        order = self.order
        all_tasks = order.tasks.all()
        total = all_tasks.count()
        done = all_tasks.filter(status='done').count()

        if total == 0:
            return
        if done == total:
            new_status = 'completed'
        elif done > 0:
            new_status = 'producing'
        else:
            new_status = 'planned'

        if order.status != new_status:
            order.status = new_status
            order.save(update_fields=['status'])


class WorkerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    stage = models.CharField(max_length=50, choices=STATION_CHOICES, verbose_name="مرحله کاری")
    skills = models.JSONField(default=list, blank=True, verbose_name="مهارت‌ها (لیست رشته‌ها)")
    skill_priority = models.JSONField(default=dict, blank=True, verbose_name="اولویت/میزان مهارت کارگر")
    is_available = models.BooleanField(default=True, verbose_name="فعال برای زمان‌بندی")
    work_start = models.TimeField(default=time(8, 0), verbose_name="شروع کار")
    work_end = models.TimeField(default=time(16, 30), verbose_name="پایان کار")
    break_start = models.TimeField(default=time(12, 30), verbose_name="شروع استراحت")
    break_end = models.TimeField(default=time(13, 30), verbose_name="پایان استراحت")
    excluded_products = models.ManyToManyField(
        'Product',
        blank=True,
        verbose_name="محصولات ممنوعه",
        help_text="کارگر در زمان‌بندی این محصولات لحاظ نمی‌شود"
    )
    excluded_items = models.ManyToManyField(
        'OrderItem',
        blank=True,
        verbose_name="آیتم‌های ممنوعه",
        help_text="کارگر روی این آیتم‌های خاص کار نخواهد کرد"
    )

    def __str__(self):
        return f"{self.user.username} - {self.get_stage_display()}"

    class Meta:
        verbose_name = "پروفایل کارگر"
        verbose_name_plural = "پروفایل کارگران"


class ProductionLog(models.Model):
    STATION_CHOICES = STATION_CHOICES
    order_item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name='logs', verbose_name="آیتم سفارش")
    stage = models.CharField(max_length=50, choices=STATION_CHOICES, verbose_name="مرحله")
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, verbose_name="کاربر")
    notes = models.CharField(max_length=200, blank=True, verbose_name="یادداشت")
    created_at = PersianDateField(auto_now_add=True, verbose_name="تاریخ ثبت")

    class Meta:
        verbose_name = "گزارش تولید"
        verbose_name_plural = "گزارش‌های تولید"






class ProductionEvent(models.Model):
    EVENT_TYPES = [
        ('started', 'شروع'),
        ('done', 'اتمام'),
        ('reassigned', 'تغییر کارگر'),
        ('status_changed', 'تغییر وضعیت دستی'),
    ]
    task = models.ForeignKey(
        'ProductionTask', on_delete=models.CASCADE, related_name='events', verbose_name="تسک تولید"
    )
    order = models.ForeignKey('Order', on_delete=models.CASCADE, related_name='production_events', verbose_name="سفارش")
    order_item = models.ForeignKey(
        'OrderItem', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='production_events', verbose_name="آیتم سفارش"
    )
    station_name = models.CharField(max_length=50, choices=STATION_CHOICES, verbose_name="ایستگاه")
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES, verbose_name="نوع رویداد")
    quantity = models.PositiveIntegerField(default=0, verbose_name="تعداد در این رویداد")
    old_status = models.CharField(max_length=20, blank=True, verbose_name="وضعیت قبلی")
    new_status = models.CharField(max_length=20, blank=True, verbose_name="وضعیت جدید")
    old_worker = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name="کارگر قبلی"
    )
    new_worker = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name="کارگر جدید"
    )
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="ثبت‌کننده")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="زمان ثبت")

    class Meta:
        verbose_name = "رویداد تولید"
        verbose_name_plural = "رویدادهای تولید"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['order_item', 'station_name']),
            models.Index(fields=['task', 'created_at']),
            models.Index(fields=['event_type', 'created_at']),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} - تسک {self.task_id} - {self.created_at}"


class ProductionDefect(models.Model):
    """A damaged part reported at a production station, before rework is issued."""
    STATUS_CHOICES = [
        ('reported', 'ثبت شده'),
        ('material_requested', 'در انتظار مواد جایگزین'),
        ('rework_issued', 'مواد جایگزین تحویل شد'),
        ('closed', 'بسته شده'),
    ]
    task = models.ForeignKey(
        ProductionTask, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='defects', verbose_name='مرحله/تسک (اختیاری)'
    )
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='defects', verbose_name='سفارش')
    order_item = models.ForeignKey(OrderItem, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='defects', verbose_name='آیتم سفارش')
    packaging_unit = models.ForeignKey(
        'PackagingUnit', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='defects', verbose_name='واحد بسته‌بندی (بارکد یکتا)'
    )
    part = models.ForeignKey(Part, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name='defects', verbose_name='قطعه آسیب‌دیده')
    color_part = models.CharField(max_length=20, choices=Color.PART_CHOICES, blank=True, verbose_name='بخش رنگی')
    quantity = models.PositiveIntegerField(default=1, verbose_name='تعداد خراب')
    description = models.TextField(verbose_name='شرح خرابی و اقدام لازم')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='reported', verbose_name='وضعیت')
    reported_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL,
                                    related_name='reported_defects', verbose_name='ثبت‌کننده')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان ثبت')

    class Meta:
        verbose_name = 'خرابی تولید'
        verbose_name_plural = 'خرابی‌های تولید'
        ordering = ['-created_at']

    def __str__(self):
        return f'خرابی {self.quantity} عددی - سفارش {self.order_id}'

    @property
    def process_name(self):
        if self.task and self.task.painting_stage:
            return self.task.painting_stage.process.name

        item = self.packaging_unit.order_item if self.packaging_unit_id else self.order_item
        color_part = self.color_part or (self.task.color_part if self.task else '')
        if not item or not color_part:
            return None

        color_obj = item.ordercolor.filter(part=color_part).first()
        code = color_obj.code if color_obj and color_obj.code and color_obj.code != 'nan' else None
        if not code:
            from .utils import _parse_default_colors, get_painting_process_for_color
            code = _parse_default_colors(item.product).get(color_part)
            process = get_painting_process_for_color(code) if code else None
        else:
            from .utils import get_painting_process_for_color
            process = get_painting_process_for_color(code)
        return process.name if process else None


class PackagingUnit(models.Model):
    order_item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name='packaging_units')
    unit_number = models.PositiveIntegerField(verbose_name="شماره واحد")
    qr_code = models.ImageField(upload_to='qr/packaging/', blank=True, null=True, verbose_name="QR بسته‌بندی")
    
    # وضعیت بسته‌بندی
    is_packed = models.BooleanField(default=False, verbose_name="بسته‌بندی شده")
    packed_at = models.DateTimeField(null=True, blank=True, verbose_name="زمان بسته‌بندی")
    packed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='packed_units', verbose_name="بسته‌بندی‌کننده")
    
    # وضعیت ارسال
    is_shipped = models.BooleanField(default=False, verbose_name="ارسال شده")
    shipped_at = models.DateTimeField(null=True, blank=True, verbose_name="زمان ارسال")
    shipped_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='shipped_units', verbose_name="ارسال‌کننده")

    class Meta:
        unique_together = ('order_item', 'unit_number')
        verbose_name = "واحد بسته‌بندی"
        verbose_name_plural = "واحدهای بسته‌بندی و ارسال"

    def __str__(self):
        return f"بسته {self.unit_number} از {self.order_item}"

    def get_absolute_url(self):
        return reverse('scan_packaging_unit', args=[self.id])








class ShipmentLog(models.Model):
    packaging_unit = models.ForeignKey(
        PackagingUnit,
        on_delete=models.CASCADE,
        related_name='shipment_logs',
        verbose_name="بسته‌بندی"
    )
    plate_number = models.CharField(max_length=50, verbose_name="پلاک")
    shipped_at = models.DateTimeField(auto_now_add=True, verbose_name="زمان ارسال")
    shipped_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="ارسال‌کننده")
    delivery_notes = models.TextField(blank=True, verbose_name="یادداشت‌های تحویل")

    class Meta:
        verbose_name = "بارگیری"
        verbose_name_plural = "بارگیری"



# ===================== مدل‌های روند نقاشی =====================

class PaintingProcess(models.Model):
    name = models.CharField(max_length=100, verbose_name="نام روند")
    code = models.CharField(max_length=20, unique=True, verbose_name="کد روند")
    color_codes = models.JSONField(default=list, verbose_name="لیست کدهای رنگی مرتبط")
    is_active = models.BooleanField(default=True, verbose_name="فعال")
    description = models.TextField(blank=True, verbose_name="توضیحات")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "روند نقاشی"
        verbose_name_plural = "روندهای نقاشی"


class PaintingStage(models.Model):
    SKILL_CHOICES = [
        ('painter', 'نقاش'),
        ('general', 'زیرکار'),
    ]
    process = models.ForeignKey(PaintingProcess, on_delete=models.CASCADE, related_name='stages')
    order = models.PositiveSmallIntegerField(verbose_name="ترتیب مرحله")
    name = models.CharField(max_length=100, verbose_name="نام مرحله")
    duration_minutes = models.PositiveIntegerField(verbose_name="زمان انجام (دقیقه)")
    drying_time_minutes = models.PositiveIntegerField(default=0, verbose_name="زمان خشک‌شدن (دقیقه)")
    required_skill = models.CharField(max_length=50, choices=SKILL_CHOICES, default='painter', verbose_name="مهارت مورد نیاز")

    class Meta:
        ordering = ['process', 'order']
        unique_together = ('process', 'order')
        verbose_name = "مرحله نقاشی"
        verbose_name_plural = "مراحل نقاشی"

    def __str__(self):
        return f"{self.process.name} - مرحله {self.order}: {self.name}"


class PaintingProcessMaterial(models.Model):
    """
    کاتالوگ مواد اولیه‌ی متعلق به یک روند نقاشی — بدون مقدار مصرف.
    فقط اعلام می‌کند «این روند اصولاً از این ماده استفاده می‌کند».
    مقدار واقعی مصرف همیشه در PaintingMaterialRequirement (سطح محصول+بخش رنگی)
    تعیین می‌شود. این دو مدل را هرگز ادغام نکن.
    """
    process = models.ForeignKey(
        'PaintingProcess',
        on_delete=models.CASCADE,
        related_name='catalog_materials',
        verbose_name="روند نقاشی",
    )
    raw_material = models.ForeignKey(
        'inventory.RawMaterial',
        on_delete=models.PROTECT,
        related_name='process_catalog_entries',
        verbose_name="مادة اولیه",
    )
    is_color_variant = models.BooleanField(
        default=False,
        verbose_name="وابسته به کد رنگ سفارش",
        help_text="اگر فعال باشد، جنس واقعی ماده بر اساس کد رنگ سفارش انتخاب می‌شود؛ مقدار مصرف در PaintingMaterialRequirement ثابت می‌ماند.",
    )

    class Meta:
        verbose_name = "ماده اولیه کاتالوگ روند"
        verbose_name_plural = "مواد اولیه کاتالوگ روندها"
        unique_together = ('process', 'raw_material')
        ordering = ['process__name', 'raw_material__name']

    def __str__(self):
        return f"{self.process.name} ← {self.raw_material.name}"


class PaintingMaterialRequirement(models.Model):
    """
    مقدار واقعی مصرف یک ماده اولیه‌ی متعلق به یک روند، برای یک (محصول + بخش رنگی)
    مشخص. هم product و هم color_part الزامی هستند — دیگر هیچ حالت پیش‌فرض/
    سراسری وجود ندارد؛ چون مثلاً دستگیره و بدنه با اینکه از یک روند عبور
    می‌کنند، مقدار مصرف کاملاً متفاوتی دارند و نمی‌توان یک مقدار مشترک گذاشت.
    """
    process = models.ForeignKey(
        'PaintingProcess',
        on_delete=models.CASCADE,
        related_name='material_requirements',
        verbose_name="روند نقاشی",
    )
    raw_material = models.ForeignKey(
        'inventory.RawMaterial',
        on_delete=models.PROTECT,
        related_name='painting_requirements',
        verbose_name="مادة اولیه",
    )
    product = models.ForeignKey(
        'Product',
        on_delete=models.CASCADE,
        related_name='painting_material_requirements',
        verbose_name="محصول",
    )
    color_part = models.CharField(
        max_length=20,
        choices=Color.PART_CHOICES,
        verbose_name="بخش رنگی",
    )
    consumption_per_unit = models.DecimalField(
        max_digits=10, decimal_places=3, default=0,
        verbose_name="مقدار مصرف به ازای هر واحد محصول",
    )

    class Meta:
        verbose_name = "فرمول مصرف مواد نقاشی"
        verbose_name_plural = "فرمول‌های مصرف مواد نقاشی"
        unique_together = ('process', 'raw_material', 'product', 'color_part')
        ordering = ['product__name', 'color_part', 'process__name', 'raw_material__name']

    def __str__(self):
        return f"{self.product} / {self.color_part} — {self.process.name} ← {self.raw_material} ({self.consumption_per_unit})"


class PaintingColorMaterialVariant(models.Model):
    """
    ماده اولیه واقعی یک اسلات رنگ‌وابسته برای هر کد رنگ سفارش.
    مقدار مصرف روی PaintingMaterialRequirement تعریف می‌شود و در این مدل تکرار نمی‌شود.
    """
    process_material = models.ForeignKey(
        PaintingProcessMaterial,
        on_delete=models.CASCADE,
        related_name='color_variants',
        limit_choices_to={'is_color_variant': True},
        verbose_name="اسلات ماده کاتالوگ روند",
    )
    color_code = models.CharField(
        max_length=20,
        choices=Color.CODE_CHOICES,
        verbose_name="کد رنگ سفارش",
    )
    raw_material = models.ForeignKey(
        'inventory.RawMaterial',
        on_delete=models.PROTECT,
        related_name='painting_color_variants',
        verbose_name="ماده اولیه واقعی",
    )

    class Meta:
        verbose_name = "نگاشت کد رنگ به ماده اولیه"
        verbose_name_plural = "نگاشت‌های کد رنگ به ماده اولیه"
        unique_together = ('process_material', 'color_code')
        ordering = ['process_material__process__name', 'process_material__raw_material__name', 'color_code']

    def __str__(self):
        return f"{self.process_material} → {self.get_color_code_display()}: {self.raw_material}"


    def clean(self):
        if self.process_material_id and not self.process_material.is_color_variant:
            raise ValidationError({
                'process_material': 'اسلات انتخاب‌شده به کد رنگ سفارش وابسته نیست.',
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class PaintingAssignmentRule(models.Model):
    """
    قوانین تخصیص دستی کارگران به مراحل نقاشی بر اساس رنگ و مرحله.
    این قوانین قبل از زمان‌بندی خودکار تنظیم می‌شوند و در الگوریتم
    زمان‌بندی برای ترجیح دادن، محدود کردن یا منع کردن کارگر مورد نظر استفاده می‌شوند.
    """
    RULE_TYPE_CHOICES = [
        ('priority', 'فقط اولویت‌دهی'),
        ('exclusive', 'محدودکننده'),
        ('exclusion', 'منع‌کننده'),
    ]
    worker = models.ForeignKey(
        WorkerProfile,
        on_delete=models.CASCADE,
        verbose_name="کارگر",
        related_name='assignment_rules'
    )
    painting_stage = models.ForeignKey(
        'PaintingStage',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name="مرحله نقاشی (اختیاری)"
    )
    color_codes = models.JSONField(
        null=True,
        blank=True,
        verbose_name="کدهای رنگ",
        help_text="لیست کدهای رنگی که این قانون برای آن‌ها اعمال می‌شود (خالی = همه رنگ‌ها)"
    )
    process = models.ForeignKey(
        PaintingProcess,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name="روند نقاشی (اختیاری)",
        help_text="محدود کردن مرحله به یک روند خاص"
    )
    rule_type = models.CharField(
        max_length=20,
        choices=RULE_TYPE_CHOICES,
        default='priority',
        verbose_name="نوع قانون",
        help_text="فقط اولویت‌دهی، محدودکننده (کارگر فقط همین تسک‌ها را بگیرد) یا منع‌کننده"
    )
    priority = models.IntegerField(
        default=100,
        verbose_name="اولویت",
        help_text="عدد بالاتر = اولویت بیشتر در زمان‌بندی (فقط برای نوع اولویت‌دهی)"
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="فعال"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "قانون تخصیص کارگر"
        verbose_name_plural = "قوانین تخصیص کارگر"
        ordering = ['-priority', 'worker']

    def __str__(self):
        parts = []
        if self.painting_stage:
            parts.append(str(self.painting_stage))
        if self.color_codes:
            parts.append(f"رنگ‌های {', '.join(self.color_codes)}")
        type_label = dict(self.RULE_TYPE_CHOICES).get(self.rule_type, self.rule_type)
        return f"{self.worker} [{type_label}] ← {' + '.join(parts)}"


def create_paint_tasks(tasks_list, order, quantity, process, base_step, order_item, color_part=''):
    """ایجاد تسک‌های نقاشی برای یک قطعه/آیتم و افزودن به task_list."""
    from .models import ProductionTask

    stages = process.stages.all().order_by('order')
    for idx, stage in enumerate(stages, start=1):
        tasks_list.append(
            ProductionTask(
                order=order,
                part=None,
                station_name='paint',
                step_order=base_step + idx,
                quantity=quantity,
                status='pending' if idx == 1 else 'waiting',
                painting_stage=stage,
                order_item=order_item,
                color_part=color_part,
            )
        )



class Holiday(models.Model):
    """
    تعطیلات رسمی و روزهای غیرکاری
    """
    date = models.DateField(
        unique=True,
        verbose_name="تاریخ میلادی",
        help_text="تاریخ دقیق تعطیلی (میلادی)"
    )
    description = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="مناسبت"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "تعطیلی"
        verbose_name_plural = "تعطیلات"
        ordering = ['date']

    def __str__(self):
        return f"{self.date} - {self.description}" if self.description else str(self.date)
