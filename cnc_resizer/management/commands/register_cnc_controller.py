"""Register (or refresh) the workshop CNC controller profile for admin approval.

The command pre-fills the auditable :class:`~cnc_resizer.models.CncControllerProfile`
row with the dialect facts reverse-engineered from the workshop's real production
files. It intentionally does NOT approve the profile: approval and validated
source hashes are a human, auditable act done in Django admin after the first
machine-verified run.

    python manage.py register_cnc_controller            # create/update, no approval
    python manage.py register_cnc_controller --rebind-existing
"""
from django.core.management.base import BaseCommand

from cnc_resizer.models import CncControllerProfile

PROFILE_KEY = "workshop_router"
ADAPTER_KEY = "minimal_xy_router"

SUPPORTED_G = ["0", "1", "2", "3", "21", "43", "53", "54", "90"]
SUPPORTED_M = ["3", "6", "30", "141"]

EVIDENCE = (
    "دیالکت از فایل‌های واقعی دستگاه استخراج شد: سربرگ G90G54 / G53 Z0 / T#M6 / M141 / "
    "M6T# / G43 H#، حرکت مطلق G0/G1/G2/G3، قوس فقط با مرکز I/J، ارتفاع‌گیر G0Z و پارک "
    "G0X0Y0 در پایان هر قطعه، پارک ماشینی G53 و M30 در پایان برنامه، شماره‌گذاری N با گام ۱۰ "
    "و پایان‌های خط CRLF. خروجی فقط با پست‌پروسسور minimal_xy_router صادر می‌شود و هر انحراف "
    "از دیالکت fail-closed رد می‌گردد. عملکرد دقیق M141 (PLC: وکیوم/هوا/دستور ATC) پیش از "
    "تولید واقعی باید با اجرای آزمایشی روی دستگاه تأیید شود."
)


class Command(BaseCommand):
    help = "ثبت/به‌روزرسانی پروفایل کنترلر CNC کارگاه (بدون تأیید؛ تأیید در ادمین انجام می‌شود)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebind-existing", action="store_true",
            help="فایل‌های CNC آرشیوشدهٔ بدون پروفایل را به این پروفایل متصل کن",
        )

    def handle(self, *args, **options):
        profile, created = CncControllerProfile.objects.update_or_create(
            key=PROFILE_KEY,
            defaults={
                "manufacturer": "Workshop (Custom)",
                "model": "Nested-based router controller",
                "firmware_version": "",
                "adapter_key": ADAPTER_KEY,
                "supported_g_codes": SUPPORTED_G,
                "supported_m_codes": SUPPORTED_M,
                "validation_evidence": EVIDENCE,
                "active": True,
            },
        )
        # approval is never set here on purpose; keep it untouched if an admin
        # has already approved, so re-running the command cannot un-approve.
        self.stdout.write(self.style.SUCCESS(
            f"{'ایجاد شد' if created else 'به‌روزرسانی شد'}: {profile.key} "
            f"(adapter={profile.adapter_key}, approved={profile.approved})"
        ))
        self.stdout.write(
            "برای فعال‌سازی کامل خروجی دستگاه: در ادمین پروفایل را تأیید کنید (approved + "
            "approved_by + validated_source_sha256) و در selvi/.env مقادیر "
            "CNC_RESIZER_IMPLICIT_UNITS=mm و CNC_RESIZER_SHEET_ADAPTER=minimal_xy_router را تنظیم کنید."
        )

        if options.get("rebind_existing"):
            from cnc_resizer.models import CncFile

            updated = CncFile.objects.filter(
                controller_profile__isnull=True, kind=CncFile.KIND_BASE,
            ).update(controller_profile=profile)
            self.stdout.write(self.style.SUCCESS(f"{updated} فایل پایه به پروفایل متصل شد."))
