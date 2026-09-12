# signals.py
import logging
from io import BytesIO

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
import qrcode

from .models import OrderItem, PackagingUnit

logger = logging.getLogger(__name__)

barcodurl = 'https://selvichoob.ir'


@receiver(post_save, sender=OrderItem)
def generate_qr_code(sender, instance, created, **kwargs):
    """تولید QR فقط در زمان ایجاد آیتم و فقط حاوی لینک اسکن"""
    if created and not instance.qr_code:
        try:
            relative_url = reverse('scan_qr', args=[instance.id])
            base_url = getattr(settings, 'SCAN_BASE_URL', barcodurl)
            full_url = f"{base_url.rstrip('/')}{relative_url}"

            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(full_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            buffer = BytesIO()
            img.save(buffer, 'PNG')

            filename = f"qr_order_{instance.order.id}_item_{instance.id}.png"
            instance.qr_code.save(filename, ContentFile(buffer.getvalue()), save=False)
            instance.save(update_fields=['qr_code'])
            logger.info(f"QR generated for OrderItem {instance.id} -> {full_url}")
        except Exception as e:
            logger.error(f"QR generation failed: {e}")


@receiver(post_save, sender=OrderItem)
def generate_packaging_qr_codes(sender, instance, created, **kwargs):
    if created and instance.quantity > 0:
        base_url = getattr(settings, 'SCAN_BASE_URL', barcodurl)
        for i in range(1, instance.quantity + 1):
            unit = PackagingUnit.objects.create(
                order_item=instance,
                unit_number=i
            )
            scan_url = reverse('scan_packaging_unit', args=[unit.id])
            full_url = f"{base_url.rstrip('/')}{scan_url}?next={reverse('item_detail', args=[instance.id])}"
            qr = qrcode.make(full_url, box_size=10, border=4)
            buffer = BytesIO()
            qr.save(buffer, format='PNG')
            filename = f"pack_qr_order_{instance.order.id}_item_{instance.id}_unit_{i}.png"
            unit.qr_code.save(filename, ContentFile(buffer.getvalue()), save=True)
