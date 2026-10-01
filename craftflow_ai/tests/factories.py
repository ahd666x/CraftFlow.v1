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
    Part,
    Product,
    ProductBOM,
    ProductCategory,
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


def make_material_sheet(name='MDF', thickness=Decimal('16.0')):
    """متریال محصول (ورق) — متمایز از RawMaterial انبار."""
    return SheetMaterial.objects.create(
        name=name, thickness=thickness, consumption_per_unit=Decimal('1')
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


def make_order(status='producing', customer_name='مشتری آزمایشی', with_items=True):
    customer, _ = Customer.objects.get_or_create(name=customer_name)
    order = Order.objects.create(
        customer=customer,
        number=f'N-{Order.objects.count() + 1}',
        status=status,
        created_at=jdatetime.date.today(),
        due_date=jdatetime.date.today() + jdatetime.timedelta(days=10),
    )
    if with_items:
        item = OrderItem.objects.create(
            order=order, product=make_product(), quantity=3,
            size='100x50', unit_price=1000,
        )
        order.items.add(item)
    return order


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