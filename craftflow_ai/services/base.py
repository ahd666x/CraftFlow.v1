"""
لایهٔ سرویس فقط-خواندنی برای لایهٔ AI.

قانون این لایه: هیچ ``.create()`` / ``.save()`` / ``.delete()`` اینجا وجود ندارد.
تمام محاسبات از منطق موجود CraftFlow بازاستفاده می‌شود:

* تعریف ایستگاه‌ها و وضعیت‌ها  → ``product.models.STATION_CHOICES`` / ``TASK_STATUS``
* منطق تأخیر سفارش          → همان قاعدهٔ ``product.views.delayed_orders``
* محاسبهٔ موجودی انبار        → ``RawMaterial.current_stock`` (روی همان CASE تکراری views)
* کمبود مواد                  → ``inventory.services._physical_for`` (گِرد کردن به بستهٔ کامل)
* نیاز مواد یک تسک            → ``inventory.services.task_material_requirements``
* پیشرفت تسک یک آیتم           → ``product.utils.get_item_task_progress_for_station``
"""