# product/management/commands/analyze_paint_scheduling.py

"""
اسکریپت تحلیل وضعیت زمان‌بندی نقاشی
برای پیدا کردن علت عدم زمان‌بندی برخی روندها

روش اجرا:
    python manage.py analyze_paint_scheduling [--item-id ITEM_ID] [--order-id ORDER_ID] [--date DATE]
"""

from django.core.management.base import BaseCommand
from django.db.models import Q, Count, Prefetch
from django.utils import timezone
import jdatetime
from datetime import datetime, timedelta
import json


class Command(BaseCommand):
    help = 'تحلیل وضعیت زمان‌بندی نقاشی برای شناسایی مشکلات'

    def add_arguments(self, parser):
        parser.add_argument(
            '--item-id',
            type=int,
            help='شناسه یک آیتم خاص برای تحلیل دقیق'
        )
        parser.add_argument(
            '--order-id',
            type=int,
            help='شناسه سفارش برای تحلیل'
        )
        parser.add_argument(
            '--date',
            type=str,
            help='تاریخ به فرمت YYYY-MM-DD'
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='خروجی به صورت JSON'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='نمایش جزئیات بیشتر'
        )

    def handle(self, *args, **options):
        from product.models import (
            ProductionTask, OrderItem, PaintingProcess, WorkerProfile,
            Color, PaintingStage, Order
        )

        self.verbose = options.get('verbose', False)
        self.json_output = options.get('json', False)
        item_id = options.get('item_id')
        order_id = options.get('order_id')
        date_str = options.get('date')

        # ============================================================
        # ۱. اطلاعات روندهای نقاشی
        # ============================================================
        self.stdout.write("\n" + "=" * 80)
        self.stdout.write("📊 تحلیل سیستم نقاشی")
        self.stdout.write("=" * 80 + "\n")

        # ۱.۱ روندهای فعال
        processes = PaintingProcess.objects.filter(is_active=True).prefetch_related('stages')
        self.stdout.write("🔹 روندهای نقاشی فعال:")
        for p in processes:
            stages = p.stages.all().order_by('order')
            stage_info = " → ".join([f"{s.order}.{s.name}({s.required_skill})" for s in stages])
            self.stdout.write(f"  - {p.name} (کد: {p.code})")
            self.stdout.write(f"    کدهای رنگی: {p.color_codes}")
            self.stdout.write(f"    مراحل: {stage_info}")
            self.stdout.write("")

        # ۱.۲ کارگران نقاشی
        workers = WorkerProfile.objects.filter(stage='paint').select_related('user')
        self.stdout.write("🔹 کارگران نقاشی:")
        for w in workers:
            self.stdout.write(f"  - {w.user.get_full_name() or w.user.username} (مهارت‌ها: {w.skills or []})")
        self.stdout.write("")

        # ============================================================
        # ۲. اطلاعات آیتم‌های خاص
        # ============================================================
        if item_id:
            self._analyze_item(item_id)
        elif order_id:
            self._analyze_order(order_id)
        else:
            # تحلیل کلی
            self._general_analysis()

        # ============================================================
        # ۳. تحلیل تسک‌های نقاشی
        # ============================================================
        self._analyze_tasks(date_str)

        # ============================================================
        # ۴. جمع‌بندی و پیشنهادات
        # ============================================================
        self._summary()

    def _analyze_item(self, item_id):
        from product.models import OrderItem, Color, ProductionTask, PaintingProcess

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(f"🔍 تحلیل دقیق آیتم #{item_id}")
        self.stdout.write("=" * 80 + "\n")

        try:
            item = OrderItem.objects.select_related(
                'order', 'product', 'product__category'
            ).prefetch_related('ordercolor', 'paint_tasks__painting_stage').get(pk=item_id)
        except OrderItem.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"❌ آیتم با شناسه {item_id} یافت نشد."))
            return

        self.stdout.write(f"📌 اطلاعات آیتم:")
        self.stdout.write(f"  - سفارش: #{item.order.id}")
        self.stdout.write(f"  - محصول: {item.product.name}")
        self.stdout.write(f"  - دسته: {item.product.category.name if item.product.category else '-'}")
        self.stdout.write(f"  - مشتری: {item.order.customer.name if item.order.customer else '-'}")
        self.stdout.write(f"  - تعداد: {item.quantity}")
        self.stdout.write(f"  - نماینده: {item.order.user.get_full_name() or item.order.user.username if item.order.user else '—'}")

        # رنگ‌ها
        self.stdout.write("\n🎨 رنگ‌های ثبت‌شده:")
        colors = item.ordercolor.all()
        if colors:
            for c in colors:
                self.stdout.write(f"  - {c.part}: {c.code}")
        else:
            # رنگ‌های پیش‌فرض
            default_colors = item.product.default_colors or {}
            if isinstance(default_colors, str):
                try:
                    import json
                    default_colors = json.loads(default_colors) or {}
                except:
                    default_colors = {}
            if default_colors:
                self.stdout.write("  (رنگ‌های پیش‌فرض محصول)")
                for part, code in default_colors.items():
                    self.stdout.write(f"  - {part}: {code}")
            else:
                self.stdout.write("  ⚠️ هیچ رنگی ثبت نشده است")

        # تطابق رنگ‌ها با روندها
        self.stdout.write("\n🔗 تطابق رنگ‌ها با روندهای نقاشی:")
        assignments = self._get_color_assignments(item)
        if assignments:
            for part_name, code in assignments:
                process = self._get_process_for_color(code)
                if process:
                    self.stdout.write(f"  ✅ {part_name}: {code} → {process.name}")
                else:
                    self.stdout.write(f"  ❌ {part_name}: {code} → هیچ روند فعالی یافت نشد")
        else:
            self.stdout.write("  ⚠️ هیچ تطابقی یافت نشد")

        # تسک‌های نقاشی
        self.stdout.write("\n📋 تسک‌های نقاشی موجود:")
        tasks = item.paint_tasks.all()
        if tasks:
            for task in tasks.order_by('step_order'):
                status_color = {
                    'pending': '🟡',
                    'waiting': '🔵',
                    'done': '✅'
                }.get(task.status, '⬜')
                self.stdout.write(
                    f"  {status_color} مرحله {task.step_order}: {task.painting_stage.name if task.painting_stage else '-'} "
                    f"({task.status}) - کارگر: {task.assigned_worker.get_full_name() or task.assigned_worker.username if task.assigned_worker else 'تخصیص‌نیافته'}"
                )
                if task.scheduled_start:
                    self.stdout.write(
                        f"     زمان: {task.scheduled_start.strftime('%H:%M')} - {task.scheduled_end.strftime('%H:%M') if task.scheduled_end else ''}"
                    )
        else:
            self.stdout.write("  ⚠️ هیچ تسک نقاشی‌ای برای این آیتم وجود ندارد")

    def _analyze_order(self, order_id):
        from product.models import Order

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(f"🔍 تحلیل سفارش #{order_id}")
        self.stdout.write("=" * 80 + "\n")

        try:
            order = Order.objects.prefetch_related(
                'items__ordercolor',
                'items__paint_tasks__painting_stage',
                'items__product'
            ).get(pk=order_id)
        except Order.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"❌ سفارش با شناسه {order_id} یافت نشد."))
            return

        self.stdout.write(f"📌 اطلاعات سفارش:")
        self.stdout.write(f"  - مشتری: {order.customer.name if order.customer else '-'}")
        self.stdout.write(f"  - نماینده: {order.user.get_full_name() or order.user.username if order.user else '—'}")
        self.stdout.write(f"  - تاریخ: {order.created_at}")
        self.stdout.write(f"  - وضعیت: {order.get_status_display()}")
        self.stdout.write(f"  - تعداد آیتم‌ها: {order.items.count()}")

        self.stdout.write("\n📋 آیتم‌های سفارش:")
        for item in order.items.all():
            self.stdout.write(f"\n  🔸 آیتم #{item.id}: {item.product.name}")
            colors = item.ordercolor.all()
            if colors:
                color_str = ", ".join([f"{c.part}:{c.code}" for c in colors])
                self.stdout.write(f"    رنگ‌ها: {color_str}")
            else:
                default_colors = item.product.default_colors or {}
                if isinstance(default_colors, str):
                    try:
                        import json
                        default_colors = json.loads(default_colors) or {}
                    except:
                        default_colors = {}
                if default_colors:
                    color_str = ", ".join([f"{p}:{c}" for p, c in default_colors.items()])
                    self.stdout.write(f"    رنگ‌های پیش‌فرض: {color_str}")
                else:
                    self.stdout.write(f"    رنگ‌ها: (هیچ)")

            tasks = item.paint_tasks.all()
            if tasks:
                self.stdout.write(f"    تسک‌های نقاشی: {tasks.count()}")
                for task in tasks:
                    self.stdout.write(
                        f"      - {task.painting_stage.name if task.painting_stage else '-'} "
                        f"({task.status}) - {task.assigned_worker.get_full_name() or task.assigned_worker.username if task.assigned_worker else 'بدون کارگر'}"
                    )
            else:
                self.stdout.write(f"    تسک‌های نقاشی: (هیچ) ⚠️")

    def _general_analysis(self):
        from product.models import OrderItem, ProductionTask, Color

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write("📊 تحلیل کلی")
        self.stdout.write("=" * 80 + "\n")

        # تعداد آیتم‌های آماده
        ready_items = OrderItem.objects.filter(
            logs__stage='mon'
        ).exclude(
            logs__stage='paint'
        ).exclude(
            logs__stage='packaging'
        ).distinct()

        self.stdout.write(f"🔹 آیتم‌های آماده نقاشی: {ready_items.count()}")

        # آیتم‌های با رنگ بدون روند
        items_with_colors = OrderItem.objects.filter(ordercolor__isnull=False).distinct()
        no_process_count = 0
        for item in items_with_colors[:20]:  # فقط ۲۰ مورد برای سرعت
            assignments = self._get_color_assignments(item)
            if not assignments:
                no_process_count += 1
        self.stdout.write(f"🔹 آیتم‌های با رنگ اما بدون روند مناسب: حداقل {no_process_count}")

        # تسک‌های بدون کارگر
        unassigned = ProductionTask.objects.filter(
            station_name='paint',
            assigned_worker__isnull=True,
            status__in=['pending', 'waiting']
        )
        self.stdout.write(f"🔹 تسک‌های نقاشی بدون کارگر: {unassigned.count()}")

        # تسک‌های زمان‌بندی‌شده
        scheduled = ProductionTask.objects.filter(
            station_name='paint',
            scheduled_start__isnull=False
        )
        self.stdout.write(f"🔹 تسک‌های نقاشی زمان‌بندی‌شده: {scheduled.count()}")

    def _analyze_tasks(self, date_str):
        from product.models import ProductionTask

        if date_str:
            try:
                y, m, d = map(int, date_str.split('-'))
                target_date = jdatetime.date(y, m, d)
            except:
                target_date = jdatetime.date.today()
        else:
            target_date = jdatetime.date.today()

        gregorian = target_date.togregorian()

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(f"📅 تحلیل تسک‌های روز {target_date.strftime('%Y-%m-%d')}")
        self.stdout.write("=" * 80 + "\n")

        tasks = ProductionTask.objects.filter(
            station_name='paint',
            scheduled_start__date=gregorian
        ).select_related(
            'order_item__product__category',
            'painting_stage',
            'assigned_worker',
            'order_item__order__user'
        ).order_by('assigned_worker_id', 'scheduled_start')

        if not tasks:
            self.stdout.write("⚠️ هیچ تسکی برای این تاریخ وجود ندارد.")
            return

        # گروه‌بندی بر اساس کارگر
        worker_groups = {}
        for task in tasks:
            worker_id = task.assigned_worker_id
            if worker_id not in worker_groups:
                worker_groups[worker_id] = {
                    'worker_name': task.assigned_worker.get_full_name() or task.assigned_worker.username if task.assigned_worker else 'تخصیص‌نیافته',
                    'tasks': []
                }
            worker_groups[worker_id]['tasks'].append(task)

        for worker_id, data in worker_groups.items():
            self.stdout.write(f"\n👤 {data['worker_name']} ({len(data['tasks'])} تسک)")
            for task in data['tasks']:
                start_str = task.scheduled_start.strftime('%H:%M') if task.scheduled_start else '---'
                end_str = task.scheduled_end.strftime('%H:%M') if task.scheduled_end else '---'
                self.stdout.write(
                    f"  - {start_str}-{end_str}: "
                    f"{task.order_item.product.name if task.order_item else 'بدون محصول'} "
                    f"({task.painting_stage.name if task.painting_stage else '-'})"
                )

    def _get_color_assignments(self, item):
        """دریافت لیست (بخش, کد رنگ) برای آیتم"""
        from product.utils import get_item_color_assignments
        return get_item_color_assignments(item)

    def _get_process_for_color(self, color_code):
        """دریافت روند نقاشی برای کد رنگ"""
        from product.utils import get_painting_process_for_color
        return get_painting_process_for_color(color_code)

    def _summary(self):
        self.stdout.write("\n" + "=" * 80)
        self.stdout.write("📌 جمع‌بندی و پیشنهادات")
        self.stdout.write("=" * 80 + "\n")

        self.stdout.write("1️⃣ اگر آیتمی رنگ دارد اما روندی برای آن یافت نشد:")
        self.stdout.write("   → در پنل ادمین، روند نقاشی با کد رنگی مربوطه ایجاد کنید.")
        self.stdout.write("   → یا کد رنگ آیتم را با یکی از کدهای موجود هماهنگ کنید.\n")

        self.stdout.write("2️⃣ اگر تسک بدون کارگر مانده:")
        self.stdout.write("   → کارگری با مهارت موردنیاز ایجاد کنید.")
        self.stdout.write("   → یا مهارت کارگر موجود را به‌روز کنید.\n")

        self.stdout.write("3️⃣ اگر تسک زمان‌بندی نشده:")
        self.stdout.write("   → ظرفیت روز را بررسی کنید.")
        self.stdout.write("   → تعداد کارگران را افزایش دهید.")
        self.stdout.write("   → زمان خشک‌شدن را کاهش دهید.\n")

        self.stdout.write("4️⃣ برای تحلیل دقیق‌تر یک آیتم خاص:")
        self.stdout.write("   → python manage.py analyze_paint_scheduling --item-id <ID>\n")

        self.stdout.write("=" * 80)