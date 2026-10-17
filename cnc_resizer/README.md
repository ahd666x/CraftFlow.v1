# CNC Resizer

این اپ برای پروژهٔ CraftFlow یکپارچه شده است.

- صفحهٔ ابزار: `/cnc/`
- API پردازش: `/cnc/api/process/`
- منطق مستقل و بدون وابستگی به Django: `core.py`
- ابزار و API فقط برای کاربران مدیر/مدیر ارشد قابل دسترسی‌اند.
- فایل‌های CNC آپلودشده در `CncFile` با SHA-256، وضعیت قابل resize و نتیجهٔ اعتبارسنجی ثبت می‌شوند.
- در زنجیرهٔ تولید، محور Y فایل CNC برابر `Part.length` در نظر گرفته شده است؛ تولید خودکار فایل مشتق در فاز بعدی به resolver متصل می‌شود.

برای اجرای نسخهٔ مستقل خارج از پروژهٔ Django:

```bash
python cnc_resizer_standalone.py
```

## گزارش قطعات با ابعاد غیرمعمول

برای شناسایی قطعاتی که طول آن‌ها بیش از ۳۰۰۰ میلی‌متر است، نسبت طول به عرض آن‌ها حداقل ۸ است، یا روی هیچ‌یک از ورق‌های کامل فعال موجود جا نمی‌شوند، دستور زیر را اجرا کنید:

```bash
python manage.py report_unusual_cnc_parts
```

فقط برای یک یا چند سفارش:

```bash
python manage.py report_unusual_cnc_parts --order-id 123 --order-id 124
```

خروجی JSON یا CSV برای بررسی و بایگانی:

```bash
python manage.py report_unusual_cnc_parts --format json > unusual-parts.json
python manage.py report_unusual_cnc_parts --format csv > unusual-parts.csv
```

حدها قابل تغییر هستند:

```bash
python manage.py report_unusual_cnc_parts --max-length 2500 --max-aspect-ratio 6
```

این گزارش هیچ قطعه یا تسکی را حذف و تغییر نمی‌دهد. در حالت «تفکیک CNC» نیز پیش از ساخت G-code، موارد غیرمعمول به‌صورت هشدار در پنل برنامه‌ریزی نمایش داده می‌شوند.
