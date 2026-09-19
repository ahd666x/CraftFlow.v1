# admin.py
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from .models import (
    Color, ProductCategory, WorkerProfile, Customer, ProductBOM, Product,
    Order, ProductionTask, Part, OrderItem, ProductionLog , PackagingUnit ,
    ProductionEvent,
)
from .forms import OrderItemForm
from .models import ShipmentLog
from .models import PaintingProcess, PaintingStage, PaintingProcessMaterial
from .models import PaintingAssignmentRule
from .models import PaintingMaterialRequirement, PaintingColorMaterialVariant

# @admin.register(PackagingUnit)
# class PackagingUnitAdmin(admin.ModelAdmin):
#     list_display = ['unit_number']
#     search_fields = ['unit_number']



@admin.register(PackagingUnit)
class PackagingUnitAdmin(admin.ModelAdmin):
    list_display = [
        'id',
        'order_item_link',
        'unit_number',
        'is_packed',
        'is_shipped',
        'packed_at',
        'shipped_at',
    ]
    list_filter = [
        'is_packed',
        'is_shipped',
    ]
    search_fields = [
        'order_item__order__id',
        'order_item__product__name',
        'order_item__order__customer__name',
    ]
    readonly_fields = [
        'packed_at',
        'packed_by',
        'shipped_at',
        'shipped_by',
        'qr_code_preview',
    ]
    fieldsets = (
        ('اطلاعات اصلی', {
            'fields': ('order_item', 'unit_number', 'qr_code_preview')
        }),
        ('وضعیت بسته‌بندی', {
            'fields': ('is_packed', 'packed_at', 'packed_by')
        }),
        ('وضعیت ارسال', {
            'fields': ('is_shipped', 'shipped_at', 'shipped_by')
        }),
    )

    @admin.display(description='آیتم سفارش')
    def order_item_link(self, obj):
        url = reverse('admin:product_orderitem_change', args=[obj.order_item.id])
        return format_html('<a href="{}">{}</a>', url, obj.order_item)

    @admin.display(description='پیش‌نمایش QR')
    def qr_code_preview(self, obj):
        if obj.qr_code:
            return format_html('<img src="{}" width="100" height="100" />', obj.qr_code.url)
        return '-'






@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = ['part', 'code', 'orderitem_link']
    list_filter = ['part']
    search_fields = ['part', 'code', 'orderitem__id']

    def orderitem_link(self, obj):
        url = reverse('admin:product_orderitem_change', args=[obj.orderitem.id])
        return format_html('<a href="{}">{}</a>', url, obj.orderitem)
    orderitem_link.short_description = 'آیتم سفارش'

@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']

@admin.register(WorkerProfile)
class WorkerProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'stage', 'is_available', 'skills']
    list_filter = ['stage', 'is_available']
    list_editable = ['is_available']
    search_fields = ['user__username', 'user__first_name', 'user__last_name']
    filter_horizontal = ['excluded_products']
    fieldsets = (
        ('اطلاعات کاربر', {
            'fields': ('user', 'stage')
        }),
        ('مهارت‌ها و اولویت‌ها', {
            'fields': ('skills', 'skill_priority')
        }),
        ('تنظیمات زمان‌بندی', {
            'fields': ('is_available', 'excluded_products'),
            'description': 'محصولاتی که این کارگر نباید در آنها کار کند'
        }),
    )

class ColorInline(admin.TabularInline):
    model = Color
    fields = ['part', 'code']
    extra = 2

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']



class ProductBOMInline(admin.TabularInline):
    model = ProductBOM
    fields = [
        'part',
        'quantity',
        'allow_material_override',
        'color_part',
        'color_material_map',
        'size_affected',
        'size_adjustment_rule',
        'get_barcode',
        'get_material',
        'get_name',
        'get_dimensions',
        'get_edges',
        'get_routing',
    ]
    readonly_fields = ['get_barcode', 'get_material', 'get_name', 'get_dimensions', 'get_edges', 'get_routing']
    verbose_name = "قطعه فنی"
    verbose_name_plural = "لیست قطعات"

    # متدهای get_... بدون تغییر باقی می‌مانند

    @admin.display(description="بارکد (F3)")
    def get_barcode(self, obj):
        return obj.part.f3

    @admin.display(description="نوع ورق")
    def get_material(self, obj):
        return obj.part.material.name

    @admin.display(description="نام قطعه (F2)")
    def get_name(self, obj):
        return obj.part.f2 or obj.part.name

    @admin.display(description="ابعاد (X×Y)")
    def get_dimensions(self, obj):
        return f"{obj.part.length} × {obj.part.width}"

    @admin.display(description="نوار لبه (PVC)")
    def get_edges(self, obj):
        p = obj.part
        edges = [e for e in [p.f4, p.f5, p.f26, p.f18] if e]
        return " | ".join(edges) or "---"

    @admin.display(description="ایستگاه")
    def get_routing(self, obj):
        return format_html('<span style="color: #2e7d32; font-weight: bold;">{}</span>', obj.part.routing_code)



@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'category', 'color', 'default_size',
        'base_price', 'price_increment_per_cm', 'parts_list_key'
    ]
    list_filter = ['category', 'color']
    search_fields = ['name', 'parts_list_key']
    inlines = [ProductBOMInline]
    list_editable = ['base_price', 'price_increment_per_cm']

    fieldsets = (
        ('اطلاعات اصلی', {
            'fields': ('category', 'name', 'color', 'parts_list_key', 'description', 'image')
        }),
        ('اندازه و قیمت', {
            'fields': (
                'default_size',
                'base_price',
                'price_increment_per_cm'
            ),

        }),
    )




class OrderItemInline(admin.TabularInline):
    model = OrderItem
    fields = ['product', 'quantity', 'size', 'notes', 'colors_link', 'print_order_link', 'print_lable_link']
    readonly_fields = ['colors_link', 'print_order_link', 'print_lable_link']
    extra = 1

    def colors_link(self, obj):
        if obj.pk:
            colors = obj.ordercolor.all()
            cc = "-".join([f"{c.part}{c.code}" for c in colors if c.code != 'nan'])
            if not cc:
                cc = "انتخاب رنگ"
            url = reverse('admin:product_orderitem_change', args=[obj.id])
            return format_html('<a href="{}" target="_blank">{}</a>', url, cc)
        return "-"
    colors_link.short_description = 'رنگ'

    def print_order_link(self, obj):
        if obj.pk:
            url = reverse('print_sheet', args=[obj.id])
            return format_html('<a href="{}" target="_blank">چاپ فرم تولید</a>', url)
        return "-"
    print_order_link.short_description = 'فرم تولید'

    def print_lable_link(self, obj):
        if obj.pk:
            url = reverse('print_lable', args=[obj.id])
            return format_html('<a href="{}" target="_blank">چاپ لیبل</a>', url)
        return "-"
    print_lable_link.short_description = 'لیبل بسته‌بندی'


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'created_at', 'due_date', 'priority', 'total_items', 'print_link']
    readonly_fields = ['print_link']
    list_filter = ['status', 'created_at', 'customer', 'priority']
    inlines = [OrderItemInline]
    actions = ['generate_tasks_action']

    def total_items(self, obj):
        return obj.items.count()
    total_items.short_description = 'تعداد آیتم‌ها'

    def print_link(self, obj):
        if obj.pk:
            url = reverse('order_print', args=[obj.id])
            return format_html('<a href="{}" target="_blank">چاپ</a>', url)
        return "-"

    @admin.display(description="وضعیت")
    def display_status(self, obj):
        colors = {'draft': '#999', 'planned': '#2196F3', 'producing': '#FF9800', 'completed': '#4CAF50'}
        return format_html(
            '<b style="color: {};">{}</b>',
            colors.get(obj.status, '#000'),
            obj.get_status_display()
        )

    @admin.action(description="ایجاد دستور تولید")
    def generate_tasks_action(self, request, queryset):
        for order in queryset:
            result = order.generate_tasks()
            if result.get('success'):
                self.message_user(request, f"  {order.id} ok ")
            else:
                self.message_user(request, f"سفارش {order.id}: {result.get('error', 'خطای ناشناخته')}", level='warning')


@admin.register(ProductionTask)
class ProductionTaskAdmin(admin.ModelAdmin):
    list_display = ['order', 'part', 'station_name', 'quantity', 'status', 'completed_at']
    list_filter = ['station_name', 'status']
    search_fields = ['part__f3', 'order__id']
    readonly_fields = ['scanned_by', 'completed_at']
    actions = ['save_tasks' , 'save_tasks_pending']

    # @admin.action(description="t")
    # def status_tasks(self, request, queryset):
    #     for order in queryset:
    #         if order.generate_tasks():
    #             self.message_user(request, f"تسک‌های سفارش {order.id} صادر شد.")
    #         else:
    #             self.message_user(request, f"سفارش {order.id} قبلاً صادر شده است.", level='warning')


    # @admin.action(description="t")
    # def save_tasks(self, request, queryset):
    #     cnc_tasks = queryset.objects.filter(
    #             order__in=orders,
    #             station_name='cnc',
    #             status='pending'
    #         )
    #         with transaction.atomic():
    #             for task in cnc_tasks:
    #                 task.status = 'done'
    #                 task.save()   # این متد مرحله بعد را فعال و وضعیت سفارش را به‌روز می‌کند

    @admin.action(description="done")
    def save_tasks(self, request, queryset):
        for task in queryset:
            task.status = 'done'
            task.save()

    @admin.action(description="pending")
    def save_tasks_pending(self, request, queryset):
        for task in queryset:
            task.status = 'pending'
            task.save()   





@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = ['f3', 'f2', 'material', 'length', 'width', 'routing_code']
    search_fields = ['f3', 'f2', 'routing_code']
    list_filter = ['material']





@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = [
        'brc', 'created_at', 'user', 'category', 'product',
        'size', 'quantity', 'calculated_price', 'color_summary',
        'print_order_link', 'print_lable_link'
    ]
    readonly_fields = ['calculated_price', 'print_order_link', 'print_lable_link']
    list_filter = ['order__user', 'product__category', 'product']
    search_fields = ['order__user__username', 'product__name']
    list_per_page = 50
    inlines = [ColorInline]

    def user(self, obj):
        return obj.order.user
    user.short_description = 'نماینده'
    user.admin_order_field = 'order__user'

    def category(self, obj):
        return obj.product.category
    category.short_description = 'دسته'
    category.admin_order_field = 'product__category'

    def size(self, obj):
        return obj.product.default_size
    size.short_description = 'اندازه'

    def created_at(self, obj):
        return obj.order.created_at
    created_at.short_description = 'تاریخ'
    created_at.admin_order_field = 'order__created_at'

    def brc(self, obj):
        return f"{obj.order.id}.{obj.id}"
    brc.short_description = 'کد سفارش'

    def color_summary(self, obj):
        return obj.color_summary
    color_summary.short_description = 'رنگ'

    @admin.display(description='قیمت محاسبه‌شده)')
    def calculated_price(self, obj):
        return f"{obj.unit_price:,} ریال"

    def print_order_link(self, obj):
        if obj.pk:
            url = reverse('print_sheet', args=[obj.id])
            return format_html('<a href="{}" target="_blank">چاپ فرم تولید</a>', url)
        return "-"

    def print_lable_link(self, obj):
        if obj.pk:
            url = reverse('print_lable', args=[obj.id])
            return format_html('<a href="{}" target="_blank">چاپ لیبل</a>', url)
        return "-"


    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change:  # اگر ویرایش باشد
            obj.sync_packaging_units()






@admin.register(ProductionLog)
class ProductionLogAdmin(admin.ModelAdmin):
    list_display = ['order_item', 'stage', 'user', 'created_at']
    list_filter = ['stage', 'order_item']



@admin.register(ShipmentLog)
class ShipmentLogAdmin(admin.ModelAdmin):
    list_display = ['plate_number', 'shipped_at', 'packaging_unit']



class PaintingProcessMaterialInline(admin.TabularInline):
    model = PaintingProcessMaterial
    fields = ('raw_material', 'is_color_variant')
    autocomplete_fields = ['raw_material']
    extra = 1
    verbose_name = "ماده اولیه کاتالوگ"
    verbose_name_plural = "کاتالوگ مواد اولیه این روند"


@admin.register(PaintingProcess)
class PaintingProcessAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'color_codes', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'code']
    fieldsets = (
        (None, {
            'fields': ('name', 'code', 'color_codes', 'is_active', 'description')
        }),
    )
    inlines = [PaintingProcessMaterialInline]


@admin.register(PaintingProcessMaterial)
class PaintingProcessMaterialAdmin(admin.ModelAdmin):
    list_display = ['process', 'raw_material', 'is_color_variant']
    list_filter = ['process', 'is_color_variant']
    search_fields = ['process__name', 'raw_material__name']
    autocomplete_fields = ['process', 'raw_material']


@admin.register(PaintingMaterialRequirement)
class PaintingMaterialRequirementAdmin(admin.ModelAdmin):
    list_display = ['product', 'color_part', 'process', 'raw_material', 'consumption_per_unit']
    list_filter = ['process', 'color_part']
    search_fields = ['product__name', 'raw_material__name']
    autocomplete_fields = ['product', 'raw_material']


@admin.register(PaintingColorMaterialVariant)
class PaintingColorMaterialVariantAdmin(admin.ModelAdmin):
    list_display = ['process_material', 'color_code', 'raw_material']
    list_filter = ['process_material__process', 'color_code']
    search_fields = ['process_material__process__name', 'process_material__raw_material__name', 'raw_material__name']
    autocomplete_fields = ['process_material', 'raw_material']


@admin.register(PaintingStage)
class PaintingStageAdmin(admin.ModelAdmin):
    list_display = ['process', 'order', 'name', 'duration_minutes', 'drying_time_minutes', 'required_skill']
    list_filter = ['process', 'required_skill']
    ordering = ['process', 'order']
    fieldsets = (
        (None, {
            'fields': ('process', 'order', 'name', 'duration_minutes', 'drying_time_minutes', 'required_skill')
        }),
    )


@admin.register(PaintingAssignmentRule)
class PaintingAssignmentRuleAdmin(admin.ModelAdmin):
    list_display = ['worker', 'painting_stage', 'color_codes', 'process', 'priority', 'is_active']
    list_filter = ['is_active', 'painting_stage__process', 'process']
    search_fields = ['worker__user__username', 'worker__user__first_name', 'worker__user__last_name']
    fieldsets = (
        (None, {
            'fields': ('worker', 'painting_stage', 'color_codes', 'process', 'priority', 'is_active')
        }),
    )


@admin.register(ProductionEvent)
class ProductionEventAdmin(admin.ModelAdmin):
    list_display = ['task', 'event_type', 'station_name', 'order', 'user', 'created_at']
    list_filter = ['event_type', 'station_name']
    search_fields = ['order__id', 'task__id']
    readonly_fields = ['created_at']