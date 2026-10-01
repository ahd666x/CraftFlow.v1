"""دادهٔ آزمایشی مشترک تست‌های craftflow_ai.

همهٔ تست‌ها از این ساختار واقعی CraftFlow استفاده می‌کنند (نام ایستگاه‌ها،
وضعیت‌ها و روابط مدل) تا تست‌ها با داده‌های واقعی هم‌راستا بمانند.
"""
from decimal import Decimal

import jdatetime
from django.contrib.auth.models import Group, User

from inventory.models import (
    MaterialIssue,
    MaterialLeftover,
    RawMaterial,
    RawMaterialCategory,
    StockMovement,
)
from product.models import (
    Customer,
    Order,
    OrderItem,
    PackagingUnit,
    Part,
    Product,
    ProductBOM,
    ProductCategory,
    ProductionDefect,
    ProductionTask,
)
from product.models import Material as SheetMaterial

PASSWORD = 'testpass'


def make_group(name):
    group, _created = Group.objects.get_or_create(name=name)
    return group


def make_user(username, groups=(), superuser=False):
    user = User.objects.create_superuser(
        username=username, password=PASSWORD, email=f'{username}@example.com'
    ) if superuser else User.objects.create_user(
        username=username, password=PASSWORD, email=f'{username}@example.com'
    )
    for group_name in groups:
        user.groups.add(make_group(group_name))
    return user


def make_material_sheet(name='MDF', thickness=Decimal('16.0'), raw_material=None):
    """متریال محصول (ورق) — متمایز از RawMaterial انبار."""
    return SheetMaterial.objects.create(
        name=name, thickness=thickness, consumption_per_unit=Decimal('1'),
        raw_material=raw_material,
    )


def make_raw_material(name='رنگ', unit='lit', stock=Decimal('0'), min_alert=Decimal('0')):
    category, _ = RawMaterialCategory.objects.get_or_create(name='رنگ‌ها')
    raw = RawMaterial.objects.create(
        category=category, name=name, unit=unit,
        min_stock_alert=min_alert, pack_size=Decimal('4'),
    )
    if stock:
        StockMovement.objects.create(
            raw_material=raw, movement_type='purchase', quantity=stock,
        )
    return raw


def make_product(name='مبل سلوی', with_bom=True):
    category, _ = ProductCategory.objects.get_or_create(name='مبلمان')
    product = Product.objects.create(category=category, name=name, base_price=1000)
    if with_bom:
        part = Part.objects.create(
            material=make_material_sheet(),
            name=f'قطعه {name}',
            length=Decimal('100.0'),
            width=Decimal('50.0'),
            pname=name,
            routing_code='cnc.prs',
        )
        ProductBOM.objects.create(product=product, part=part, quantity=2)
    return product


def make_order(status='producing', customer_name='مشتری آزمایشی', with_items=True,
               due_in_days=10, created_at=None):
    """
    ساخت سفارش با داده‌های واقعی CraftFlow.

    ``due_in_days=None`` یعنی ``due_date`` خالی است — همان وضعیتی که در دادهٔ
    فعلی کارخانه برای همهٔ سفارش‌ها برقرار است و برای تست اطمینان ``low``
    در تحلیل تأخیر لازم است.
    """
    customer, _ = Customer.objects.get_or_create(name=customer_name)
    due_date = (
        jdatetime.date.today() + jdatetime.timedelta(days=due_in_days)
        if due_in_days is not None else None
    )
    order = Order.objects.create(
        customer=customer,
        number=f'N-{Order.objects.count() + 1}',
        status=status,
        created_at=created_at or jdatetime.date.today(),
        due_date=due_date,
    )
    if with_items:
        item = OrderItem.objects.create(
            order=order, product=make_product(), quantity=3,
            size='100x50', unit_price=1000,
        )
        order.items.add(item)
    return order


def make_defect(order, status='reported', quantity=1, task=None):
    """خرابی تولید — برای تست دامنهٔ کیفیت."""
    return ProductionDefect.objects.create(
        order=order,
        order_item=order.items.first(),
        task=task,
        color_part='بدنه',
        quantity=quantity,
        description='خرابی آزمایشی',
        status=status,
    )


def make_packaging_units(item, count=3, packed=0, shipped=0):
    """
    واحدهای بسته‌بندی با وضعیت مشخص.

    توجه: سیگنال ``post_save`` روی ``OrderItem`` در خود CraftFlow به‌طور خودکار
    ``quantity`` واحد بسته‌بندی می‌سازد؛ بنابراین این کمکی آن‌ها را به‌روزرسانی
    می‌کند و واحد تکراری نمی‌سازد (قید ``unique(order_item, unit_number)``).
    """
    item.sync_packaging_units()
    units = list(
        PackagingUnit.objects
        .filter(order_item=item)
        .order_by('unit_number')[:max(0, int(count))]
    )
    for index, unit in enumerate(units, start=1):
        unit.is_packed = index <= packed
        unit.is_shipped = index <= shipped
        unit.save(update_fields=['is_packed', 'is_shipped'])
    return units


def make_tasks(order, stations=('cut', 'cnc'), status='pending', statuses=None):
    """
    ایجاد تسک برای ایستگاه‌های واقعی پروژه.

    ``statuses`` اگر داده شود، وضعیت هر ایستگاه را صریح تعیین می‌کند؛
    در غیر این صورت اولی «آمادهٔ انجام» و بقیه «در انتظار مرحلهٔ قبل» می‌شوند
    (همان رفتاری که ``Order.generate_tasks`` در CraftFlow دارد).
    """
    item = order.items.first()
    if statuses is None:
        statuses = [
            status if index == 1 else 'waiting'
            for index in range(1, len(stations) + 1)
        ]
    tasks = []
    for index, station in enumerate(stations, start=1):
        tasks.append(ProductionTask.objects.create(
            order=order,
            order_item=item,
            station_name=station,
            step_order=index,
            quantity=6,
            status=statuses[index - 1],
        ))
    return tasks


def make_open_issue(raw, order, quantity=Decimal('8')):
    """درخواست مواد باز (status=requested) برای آزمون کمبود."""
    return MaterialIssue.objects.create(
        task=order.tasks.first(),
        raw_material=raw,
        requested_quantity=quantity,
        issued_quantity=Decimal('0'),
        purpose='production',
        status='requested',
    )


def make_leftover(raw, quantity):
    row, _ = MaterialLeftover.objects.get_or_create(raw_material=raw)
    row.quantity = quantity
    row.save()
    return row