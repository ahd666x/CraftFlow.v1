"""ساخت دادهٔ نمونهٔ واقعی در دیتابیس توسعه برای تست دستی صفحهٔ امانت."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from inventory.models import (
    MaterialCustody, MaterialIssue, RawMaterial, RawMaterialCategory, StockMovement,
)
from product.models import (
    Color, Customer, Order, OrderItem, PaintingProcess, Product, ProductCategory,
    ProductionDefect,
)


class Command(BaseCommand):
    help = 'ساخت دادهٔ نمونه برای صفحهٔ تحویل روزانه نقاشی'

    def handle(self, *args, **options):
        wh, _ = User.objects.get_or_create(
            username='wh1', defaults={'is_staff': True, 'is_superuser': True})
        wh.set_password('testpass')
        wh.is_staff = True
        wh.is_superuser = True
        wh.save()

        worker, _ = User.objects.get_or_create(username='ali1', defaults={'first_name': 'علی'})
        worker.set_password('testpass')
        worker.save()

        cat, _ = RawMaterialCategory.objects.get_or_create(name='مواد نقاشی')
        raw, _ = RawMaterial.objects.get_or_create(
            category=cat, name='رنگ سفید', code='PNT1',
            defaults={'unit': 'kg', 'pack_size': Decimal('4.50')},
        )

        pcat, _ = ProductCategory.objects.get_or_create(name='مبلمان')
        product, _ = Product.objects.get_or_create(
            category=pcat, name='صندلی', defaults={'base_price': 1_000_000})
        customer, _ = Customer.objects.get_or_create(name='مشتری تست', defaults={'phone': '09120000000'})
        order, _ = Order.objects.get_or_create(number='T1', defaults={'user': wh, 'customer': customer})
        item, _ = OrderItem.objects.get_or_create(order=order, product=product, defaults={'quantity': 4})
        Color.objects.get_or_create(part='بدنه', code='8', orderitem=item)
        PaintingProcess.objects.get_or_create(name='روند نقاشی', code='T1', defaults={'color_codes': ['8']})

        if not StockMovement.objects.filter(raw_material=raw, movement_type='purchase').exists():
            StockMovement.objects.create(
                raw_material=raw, movement_type='purchase',
                quantity=Decimal('100'), note='موجودی اولیه')

        # یک امانت باز بساز: ۳ کیلو نیاز از بستهٔ ۴.۵ => ۱.۵ باقی نزد کارگر
        MaterialCustody.objects.update_or_create(
            raw_material=raw, held_by=worker,
            defaults={'quantity': Decimal('2.50')},
        )

        self.stdout.write(self.style.SUCCESS(
            f'آماده است. ورود: wh1/testpass — امانت {raw.name} نزد {worker.get_full_name() or worker.username}'
        ))