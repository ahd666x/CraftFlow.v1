from django.core.management.base import BaseCommand
from django.db import connection, transaction
from datetime import date
import jdatetime

from product.models import Order


class Command(BaseCommand):
    help = 'تعمیر تاریخ‌های شمسی که به اشتباه به صورت میلادی ذخیره شده‌اند'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='اعمال تغییرات در دیتابیس. پیش‌فرد فقط گزارش است.',
        )

    def handle(self, *args, **options):
        apply = options.get('apply', False)
        table = Order._meta.db_table
        cols = ['id', 'created_at', 'due_date']

        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT id, CAST(created_at AS TEXT), CAST(due_date AS TEXT) FROM {table}'
            )
            rows = cursor.fetchall()

        candidates = []
        for row in rows:
            record_id = row[0]
            for col, raw in zip(['created_at', 'due_date'], row[1:]):
                if not raw:
                    continue
                # The stored value is a string. If its year is in the Persian range
                # (1300-1500) it is almost certainly a Persian date stored as text.
                try:
                    y, m, d = map(int, raw.split('-'))
                except (ValueError, TypeError):
                    continue
                if not (1300 <= y <= 1500):
                    continue
                try:
                    correct = jdatetime.date(y, m, d).togregorian().isoformat()
                except Exception:
                    continue
                candidates.append((record_id, col, raw, correct))

        for rec_id, col, current, proposed in candidates:
            self.stdout.write(f'id={rec_id} {col}: {current} -> {proposed}')

        if apply and candidates:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    for rec_id, col, current, proposed in candidates:
                        cursor.execute(
                            f'UPDATE {table} SET {col} = ? WHERE id = ?',
                            [proposed, rec_id],
                        )
            self.stdout.write(self.style.SUCCESS(f'{len(candidates)} رکورد اصلاح شد.'))
        elif apply:
            self.stdout.write('هیچ رکوردی برای اصلاح یافت نشد.')
        else:
            self.stdout.write('حالت گزارش: هیچ تغییری اعمال نشد (برای اعمال --apply را بزنید).')