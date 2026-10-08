# STOREFRONT_PORT_MAP.md — ممیزی پورت فروشگاه از `store` به CraftFlow

**تاریخ:** ۱۴۰۵/۰۷/۱۴
**مبدأ:** `C:\Users\Hossein Nezhad\Desktop\store\store` — Django 5.2، معماری `apps/`
**هدف:** `C:\Users\Hossein Nezhad\Desktop\CraftFlow V1\CraftFlow` — اپ‌های `product`، `inventory`، `craftflow_ai`
**هدف این سند:** تعیین دقیق اینکه از هر فایل مبدأ چه کاری شود، **پیش از** هر تغییر کد.

---

## ۰) خلاصه تصمیم

پورت **فقط لایه‌ی تجارت/فروشگاه**. منطق تولید CraftFlow دست‌نخورده می‌ماند، چون نسخه‌ی `store` قدیمی‌تر و فقیرتر است (صف روزانه، مهارت کارگر، خرابی، AI در CraftFlow وجود دارد و در `store` نیست).

| برچسب | معنی | تعداد |
|---|---|---|
| `PORT` | کپی عین‌به‌عین، بدون تغییر معنا | ۹ |
| `ADAPT` | کپی + اصلاح اجباری برای هماهنگی با CraftFlow | ۲۱ |
| `SKIP` | اصلاً نیاید | ۲۲ |

---

## ۱) موانع بحرانی که باید قبل از هر کپی حل شوند

### B1 — `manage.py check` در حال حاضر شکست است ⛔

```
ImportError: cannot import name 'MaterialCustody' from 'inventory.models'
craftflow_ai/services/queries.py:14-15  →  from inventory.models import (MaterialCustody, …)
```

`inventory/models.py` در refactor جاری ۱۰ مدل حذف شده ولی `craftflow_ai` هنوز به یکی از آن‌ها ارجاع می‌دهد. **تا رفع این، `makemigrations` و کل تست‌ها اجرا نمی‌شود و baseline قابل اعتماد نیست.**

مدل‌های حذف‌شده: `Supplier`, `MaterialLeftover`, `MaterialCustody`, `MaterialCustodyReturn`, `CustodyConsumption`, `MaterialHandover`, `MaterialHandoverLine`, `MaterialIssue`, `PurchaseOrder`, `PurchaseOrderItem`.

> ⚠️ این تغییرات **commit نشده** و یک جلسه‌ی دیگر هم‌زمان روی همین شاخه کار می‌کند (`inventory/views.py` و `inventory/templates/inventory/base.html` در لحظه‌ی ممیزی در حال تغییر بودند). پیش از شروع پورت باید تکلیف آن کار روشن شود.

### B2 — نسخه‌ی Django واقعی با `requirements.txt` نمی‌خواند ⛔

```
requirements.txt  →  Django==6.0.6
محیط واقعی         →  Django 5.2.11   (C:\Python310\lib\site-packages\django)
```

کل ممیزی سازگاری «Django 6.0» عملاً بی‌اثر است: کد روی **۵.۲** اجرا می‌شود. مبدأ هم `Django>=5.2,<6.0` است. یعنی هر دو پروژه روی ۵.۲ هستند و کد مبدأ با محیط فعلی سازگار است — ولی pin اشتباه در `requirements.txt` باید اصلاح شود، وگرنه `pip install -r` پروژه را می‌شکند.

### B3 — تصمیم `AUTH_USER_MODEL` (غیرقابل‌دورزدن)

مبدأ `AUTH_USER_MODEL = 'accounts.User'` دارد و کد مبدأ به آن وابسته است. CraftFlow کاربر اختصاصی **ندارد** و از `django.contrib.auth.models.User` استفاده می‌کند.

**توصیه: `AUTH_USER_MODEL` را تعویض نکن.** جایگزینی یعنی دست‌کم ۱۵ FK موجود + مهاجرت داده روی دیتابیس واقعی. به‌جایش اپ `accounts` روی همان User فعلی ساخته می‌شود (`OTPRequest`, `Wishlist`, `WishlistItem`) و ورود با شماره/ایمیل از طریق auth backend انجام می‌شود، نه با مدل کاربر جدید.

### B4 — برخورد نام جدول `Color` (بمب ساکت)

مبدأ و هدف **هر دو** مدلی به نام `Color` دارند با معنای کاملاً متفاوت:

| | مبدأ `catalog.Color` | هدف `product.Color` |
|---|---|---|
| فیلدها | `name`, `code` | `part`, `code`, `orderitem`, `hex_code`, `material_name` |
| معنا | پالت رنگ سراسری | رکورد رنگ انتخابیِ هر آیتم سفارش |

جدول هر دو `color` می‌شود. کپی مستقیم = `ConflictingModel` یا رکوردهای درهم‌ریخته. راه‌حل: مدل پالت با **نام دیگر** (`ColorOption`) منتقل شود.

---

## ۲) مقایسه‌ی مدل‌ها (مهم‌ترین بخش)

### `product.Product` — ۱۲ فیلد کم دارد

| فیلد مبدأ | نوع | وضعیت در CraftFlow |
|---|---|---|
| `slug` | `SlugField(allow_unicode=True)` | ❌ ندارد — برای URL فارسی **ضروری** است |
| `price` | `Decimal(12,0)` | ❌ ندارد |
| `length`,`width`,`height` | `PositiveIntegerField(null)` | ❌ ندارد |
| `length_editable`,`width_editable`,`height_editable` | `Boolean(default=True)` | ❌ ندارد |
| `length_price_percent`,`width_price_percent`,`height_price_percent` | `Decimal(5,2)` | ❌ ندارد |
| `stock` | `PositiveInteger(default=0)` | ❌ ندارد |
| `price_increment_per_cm` | `Decimal(5,2)` | ✅ دارد ولی **تک‌بعدی** — با ۳ محور جایگزین می‌شود |
| `image` | `ImageField` | ✅ دارد — با `ProductImage` گالری کامل می‌شود |

⚠️ **سیاست `stock` یک تصمیم محصول است، نه فنی.** CraftFlow انبارِ *ماده اولیه* دارد (`inventory.RawMaterial`) نه کالای تمام‌شده؛ یعنی تولید به‌مینا (`make-to-order`). افزودن `stock` بدون تعریف معنایش، موجودی را نادرست نشان می‌دهد. گزینه‌ها: `is_made_to_order` بدون عدد موجودی، یا شمارنده‌ی سفارش در جریان.

### `product.Order` — ۱۰ فیلد کم دارد (کل لایه‌ی پول)

`total_amount`, `discount_amount`, `final_amount`, `discount`(FK), `address`(FK), `shipping_address`, `tracking_code`, `paid_at` + چهار وضعیت `paid`/`shipped`/`delivered`/`cancelled`.

همه **افزایشی**‌اند و روی داده‌ی موجود امن‌اند.

⚠️ **هشدار:** `product/models.py:867` متد `update_order_status()` وضعیت را از روی تسک‌ها بازمحاسبه می‌کند و **هیچ قفلی ندارد**. مبدأ معادلش (`orders/models.py:423`) یک `LOCKED_STATUSES` دارد که از بازنویسی وضعیت‌های مالی جلوگیری می‌کند. تا وقتی وضعیت‌های پولی اضافه نشده باشد بی‌خطر است؛ **به‌محض اضافه شدن باید آن guard اضافه شود، وگرنه تسک‌ها وضعیت `paid` را پاک می‌کنند.**

### `product.OrderItem` — ۶ فیلد کم دارد

`length`,`width`,`height` (ابعاد سفارشی مشتری) + `created_at`,`updated_at`,`is_active`.

⚠️ `calculate_price()` دو پروژه **دو الگوریتم متفاوت** دارند:
- هدف (`product/models.py:408`): فقط **اولین عدد** رشته‌ی `size` را با `re` درمی‌آورد، عرض/ارتفاع را اصلاً نمی‌بیند.
- مبدأ: سه محور مستقل، هرکدام با درصد و فلگ `editable` خودش.

پورت `pricing.py` مبدأ این را **بهبود** می‌دهد، ولی قیمت سفارش‌های موجود را تغییر می‌دهد → نیازمند تست رگرسیون.

### مدل‌های کاملاً جدید

`ProductImage`, `ProductReview`, `StockAlert`, `ComparisonList`, `Address`, `ReturnRequest`, `Discount`, `Payment`, `Transaction`, `UserProfile/OTP`, `Wishlist`, `WishlistItem`

---

## ۳) جدول PORT / ADAPT / SKIP

### ۳-۱) `PORT` — کپی عین‌به‌عین

| فایل مبدأ | مقصد |
|---|---|
| `apps/catalog/pricing.py` | `storefront/pricing.py` |
| `apps/orders/production_utils.py` | `storefront/production_utils.py` |
| `apps/common/converters.py` | `storefront/converters.py` |
| `apps/common/managers.py` | `storefront/managers.py` |
| `apps/common/models.py` (`BaseModel`) | `storefront/models.py` (abstract) |
| `apps/common/templatetags/jformat.py` | `storefront/templatetags/` |
| `apps/common/templatetags/price_format.py` | `storefront/templatetags/` |
| `apps/common/templatetags/recommendations.py` | `storefront/templatetags/` |
| `templates/components/**` (۲۶ فایل) | `storefront/templates/storefront/components/` |

> `pricing.py` و `production_utils.py` **هیچ importـی از Django ندارند** و تمیزترین فایل‌های مبدأ‌اند. ارزیاب امن، sandbox واقعی AST دارد و قابل کپی است.

### ۳-۲) `ADAPT` — نیازمند اصلاح

| فایل مبدأ | مقصد | اصلاح اجباری |
|---|---|---|
| `apps/cart/models.py` | `cart/models.py` | FK محصول → `product.Product`؛ `BaseModel` → ورودی |
| `apps/cart/services.py` | `cart/services.py` | همان بالا؛ منطق `stock` بازبینی شود |
| `apps/cart/views.py` | `cart/views.py` | namespace قالب‌ها؛ `{% static %}` |
| `apps/cart/urls.py` | `cart/urls.py` | مسیرها |
| `apps/cart/context_processors.py` | `cart/context_processors.py` | ثبت در `settings` |
| `apps/catalog/views.py` | `storefront/views.py` | `redirect('accounts:login')`؛ حذف importهای بلااستفاده |
| `apps/catalog/urls.py` | `storefront/urls.py` | ثبت converter `uslug` |
| `apps/catalog/context_processors.py` | `storefront/context_processors.py` | فیلتر `is_active` |
| `apps/catalog/models.py` | `product/models.py` | **فقط** مدل‌های جدید؛ `Color` تغییر نام |
| `apps/catalog/admin.py` | `storefront/admin.py` | ثبت جداگانه |
| `apps/discounts/*` | `discounts/` | تقریباً as-is؛ FK محصول |
| `apps/orders/services.py` | `storefront/checkout.py` | **از `Order.generate_tasks()` خود CraftFlow استفاده کند**، نه نسخه‌ی مبدأ |
| `apps/orders/views.py` | `storefront/order_views.py` | namespace، مجوزها |
| `apps/orders/urls.py` | `storefront/urls.py` | namespace |
| `apps/orders/forms.py` | `storefront/forms.py` | رفع باگ `request` |
| `apps/payments/gateways/zarinpal.py` | `payments/gateways/zarinpal.py` | **بازنویسی تنظیمات** (B5) |
| `apps/payments/gateways/cod.py` | `payments/gateways/cod.py` | همان |
| `apps/payments/views.py` | `payments/views.py` | **رفع ۴ باگ بحرانی** (B5) |
| `apps/payments/models.py` | `payments/models.py` | FK → `product.Order` |
| `apps/accounts/backends.py` | `accounts/backends.py` | `get_user_model()` بماند |
| `apps/accounts/forms.py` | `accounts/forms.py` | بدون `AbstractUser` سفارشی |
| `templates/layouts/store.html` | `storefront/templates/storefront/layouts/store.html` | namespace دارایی‌ها |
| `templates/includes/*` (۵) | `storefront/templates/storefront/includes/` | همان |
| `templates/{home,catalog,cart,orders,accounts,payments}/**` | `storefront/templates/storefront/…` | **namespace اجباری** |
| `static/css/style.css` + `tailwind.config.js` | `storefront/static/storefront/css/` | مسیر content globs |
| `static/js/store/*`,`components/*`,`core/*`,`vendor/*` | `storefront/static/storefront/js/` | بازنویسی `{% static %}` |

### ۳-۳) `SKIP` — هرگز نیاید

| فایل مبدأ | دلیل |
|---|---|
| `apps/production/**` (کل پوشه) | CraftFlow نسخه‌ی کامل‌تر دارد؛ این نسخه فقیرتر است |
| `apps/orders/models.py` → `ProductionTask`,`Part`,`ProductBOM`,`ProductionLog`,`ShipmentLog`,`OrderColor` | تکراری و قدیمی |
| `templates/production/**` | قدیمی |
| `config/settings/**` | تنظیمات CraftFlow دست‌نخورده |
| `apps/api/**` | فاز بعد (DRF از قبل نصب است) |
| `apps/orders/workflows.py` | **کلاس مرده** — تأیید شد که هیچ‌جا صدا زده نمی‌شود |
| `apps/orders/signals.py`, `apps/payments/signals.py` | به `communications` وابسته‌اند، بی‌مصرف |
| `apps/accounts/services.py` | هیچ‌جا فراخوانی نمی‌شود |
| `apps/communications/**` | مدل اعلان sink بدون هیچ مکانیزم ارسال |
| `apps/orders/admin.py` | `sync_packaging_units()` را صدا می‌زند ولی در مبدأ وجود ندارد → `AttributeError` |
| `apps/catalog/services.py` | کاملاً بلااستفاده |
| `apps/catalog/signals.py` | ⛔ **باگ فعال** (B6) |
| `apps/common/fields.py` | تکراری — `product/fields.py` بهتر است |
| `templates/accounts/{register,otp_request,otp_verify,password_reset*}.html` | وابسته به User اختصاصی |
| `templates/discounts/**` | درگیر مجوز staff |
| `db.sqlite3`, `venv/`, `node_modules/`, `logs/` | مصنوعات |
| `*.md`, `audit_*.py`, `PHASE_*_REPORT.md` | مستندات مخصوص مبدأ |
| `Desktop/store/ax/` | رفرنس طراحی و عکس، نه کد |

---

## ۴) باگ‌های کد مبدأ که هنگام پورت باید اصلاح شوند

این‌ها **در مبدأ زنده‌اند**. کپی کردن بدون اصلاح، همان باگ را به CraftFlow می‌آورد.

| # | شدت | محل | مشکل |
|---|---|---|---|
| B6 | 🔴 مسدودکننده | `catalog/signals.py:53` | `ColorMaterialMap` استفاده شده ولی **import نشده** (خط ۶ فقط `Product, StockAlert, Part`). شرط خط ۵۲ روی هر ذخیره‌ی بدون تغییر متریال true است → `NameError` و شکست `save()` |
| B7 | 🔴 بحرانی | `payments/views.py:87` و `:130` | `select_for_update()` روی SQLite → `NotSupportedError`. چون داخل `atomic` است و **قبل** از آن `order.status='paid'` ست شده، کاربر خطای ۵۰۰ روی سفارشِ پرداخت‌شده می‌بیند |
| B8 | 🔴 امنیتی | `payments/views.py` | callback **تک‌بار مصرف نیست**. نبود guard یعنی تکرار همان URL، `used_count` و `stock` را دوباره کم می‌کند |
| B9 | 🟠 بالا | `gateways/zarinpal.py:11-13` | شاخه‌ی sandbox از پروتکل v10 استفاده می‌کند ولی کد فقط v4 را می‌فهمد → **مسیر sandbox کارا نیست** |
| B10 | 🟠 بالا | `gateways/zarinpal.py:8-9` | `ZARINPAL_MERCHANT_ID` و `ZARINPAL_SANDBOX` در هیچ settings فایلی تعریف نشده‌اند → همیشه `''` و همیشه sandbox |
| B11 | 🟠 بالا | `accounts/views.py:119,136` | از `timezone.timedelta` استفاده می‌کند؛ این فقط یک re-export غیررسمی است، نه API عمومی. با `datetime.timedelta` جایگزین شود |
| B12 | 🟠 بالا | `accounts/models.py:9` | `email = EmailField(unique=True)` بدون `blank=True` → ساخت خودکار کاربر در مسیر OTP شکست می‌خورد |
| B13 | 🟡 متوسط | `catalog/views.py:5` | `from django.db.models import IntegrityError` — از محل اشتباه؛ باید `django.db` |
| B14 | 🟡 متوسط | `catalog/views.py` | `render` و `Min`/`Max` import شده ولی استفاده نشده |
| B15 | 🟡 متوسط | `catalog/context_processors.py` | از `objects` استفاده می‌کند نه `active_objects` → دسته‌ی غیرفعال در منو دیده می‌شود |
| B16 | 🟡 متوسط | `orders/forms.py:13` | `request` را از kwargs می‌خواند ولی ویو آن را پاس نمی‌دهد |
| B17 | 🟡 متوسط | `discounts/views.py` | فقط `LoginRequiredMixin` دارد، **بدون بررسی staff** → هر کاربر لاگین‌شده کد تخفیف می‌سازد |
| B18 | 🟢 کم | `orders/models.py:249` | QR به‌صورت متن ساده ذخیره می‌شود، نه URL — در CraftFlow `signals.py:24` URL واقعی می‌سازد؛ **بهتر است از نسخه‌ی CraftFlow استفاده شود** |
| B19 | 🟢 کم | `catalog/models.py:25,99` | حلقه‌ی `while … .exists()` برای یکتایی slug → یک کوئری به‌ازای هر برخورد |

---

## ۵) سازگاری Django

**نتیجه ممیزی:** صفر مورد از ۱۱ الگوی حذف‌شده در Django 6.0 یافت شد:
`index_together`, `USE_L10N`, `timezone.utc`, `make_random_password`, `NullBooleanField`, `DEFAULT_FILE_STORAGE`, `STATICFILES_STORAGE`, `providing_args`, `get_storage_class`, `models.permalink`, `conf.urls.url()`.

تنها تطابق ظاهری `from django.conf.urls.static import static` در `config/urls.py:4` است که هرگز حذف نشده.

> با این حال چون محیط واقعی **۵.۲.۱۱** است (B2)، معیار درست‌بودن، سازگاری با ۵.۲ است نه ۶.۰ — و مبدأ خودش `>=5.2,<6.0` است، پس کد مبدأ با محیط فعلی سازگار است.

**تنها مانع واقعی SQLite:** `select_for_update()` در `payments/views.py` (B7). موارد دیگر پرتابل‌اند: `bulk_create` ساده، `distinct()` بدون آرگومان، `JSONField`، `models.Index`.

---

## ۶) برخورد namespace (ریسک خاموش)

| قالب/فایل | مبدأ | هدف | راه‌حل |
|---|---|---|---|
| `base.html` | `templates/base.html` (ریشه) | `product/templates/base.html` | اپ `storefront` **نباید** `base.html` تعریف کند؛ همه‌ی قالب‌ها زیر `storefront/` بروند |
| مسیر قالب‌ها | `TEMPLATES.DIRS = [BASE_DIR/'templates']` | CraftFlow: `DIRS = []` | قالب‌ها داخل خود اپ، نه ریشه — وگرنه `DIRS` قبل از `APP_DIRS` جست‌وجو می‌شود و قالب اشتباه برنده می‌شود |
| `static/fonts/` | دارد | `static/fonts/` دارد | فونت‌ها زیر `storefront/static/storefront/fonts/` |
| `static/css/style.css` | دارد | وجود ندارد | زیر `storefront/static/storefront/css/` |
| Tailwind build | `tailwind.config.js` ریشه | ندارد | بازنویسی `content` به `./storefront/**` و خروجی به مسیر اپ |

**قاعده:** هیچ‌چیز فروشگاهی نباید به ریشه‌ی پروژه بنشیند. `storefront` خودش namespace دارد.

---

## ۷) ریسک تداخل CSS

دو framework متفاوت: فروشگاه = Tailwind + Alpine + HTMX؛ پنل = Bootstrap RTL + jQuery + Select2.

**قاعده‌ی اجرایی:** layout فروشگاه فقط Tailwind، layout پنل فقط Bootstrap. هرگز در یک صفحه. ریسک اصلی reset پیش‌فرض Tailwind است که Bootstrap را می‌شکند و برعکس — اگر اشتباهی با هم load شوند، کل پنل تولید خراب می‌شود.

---

## ۸) ترتیب اجرای پیشنهادی

| فاز | کار | پیش‌شرط |
|---|---|---|
| **۰** | همین ممیزی + رفع B1 و B2 | — |
| **۱** | `storefront` پایه + فیلدهای `Product`/`Order`/`OrderItem` + `pricing.py` | ✅ `manage.py check` سبز |
| **۲** | اپ `cart` | فاز ۱ |
| **۳** | کاتالوگ + قالب‌ها (namespace‌دار) | فاز ۲ |
| **۴** | checkout → `product.Order` با `Order.generate_tasks()` خود CraftFlow | فاز ۳ |
| **۵** | `payments` + `discounts` + آدرس (با اصلاح B7-B10) | فاز ۴ |
| **۶** | `accounts` بدون تعویض `AUTH_USER_MODEL` | فاز ۵ |
| **۷** | QA و تصمیم درباره‌ی `product/templates/customer/` | فاز ۶ |

**در هر فاز:** `python manage.py check` + اجرای ۲۹ فایل تست موجود `product/tests/` باید سبز بماند. تست رگرسیون قیمت الزامی است چون `calculate_price()` تغییر می‌کند.

---

## ۹) پرسش‌های باز که باید از کاربر پرسیده شود

1. **وضعیت refactor جاری `inventory`** — کارِ در جریانِ کس دیگری است؟ تا کی باید صبر کرد؟ (B1)
2. **`stock`** — محصولات sell-void هستند یا تولید-on-order؟ معنای عدد موجودی چیست؟
3. **نسخه‌ی Django** — `requirements.txt` اشتباه است یا محیط؟ (B2)
4. **پرداخت** — زرین‌پال واقعی لازم است یا فعلاً پرداخت در محلی/دفتری کافی است؟ با توجه به B9/B10 مسیر درگاه در مبدأ آماده‌ی production نیست.
5. **دامنه‌ی فاز ۱** — آیا فاز ۱ فقط زیرساخت و مدل باشد، یا همراه با رابط کاربری؟

---

## ۱۰) وضعیت اجرا (به‌روزرسانی ۱۴۰۵/۰۷/۱۵)

### تصمیم‌های قطعی‌شده
- **B2 رفع شد:** `requirements.txt` → `Django==5.2.11` (هم‌خوان با محیط).
- **`stock` حذف شد (سؤال ۲):** تولید make-to-order است؛ هیچ فیلد موجودی در `storefront`/`cart` ساخته نشد.
- **`inventory` خارج از محدوده (سؤال ۱):** کد فروشگاه به آن وابسته نیست؛ خطاهای باقی‌مانده‌ی تست‌های آن را لمس نمی‌کنیم.
- **فاز ۱ همراه با UI بود (سؤال ۵):** قالب‌ها و staticها ساخته شدند.
- **B3 رعایت شد:** `AUTH_USER_MODEL` تعویض نشد.
- **B4 رعایت شد:** پالت با نام `ColorOption` منتقل شد، `product.Color` دست‌نخورده.

### اجرا‌شده
- فاز ۰–۴ کامل: `storefront` پایه + فیلدهای `Product`/`Order`/`OrderItem` + `pricing.py` + اپ `cart` + کاتالوگ و قالب‌ها + checkout که از `Order.generate_tasks()` خود CraftFlow استفاده می‌کند.
- migrationها ایجاد و روی `db.sqlite3` اعمال شدند (slug ۸۷ محصول و ۸ دسته backfill شد).
- URLها زیر `/shop/` و `/cart/` سوار شدند؛ context processor سبد ثبت شد.
- Tailwind با CLI محلی مبدأ (`node_modules/tailwindcss` ۳.۴.۱۹) کامپایل شد؛ خروجی `storefront/static/storefront/css/style.css`. فونت‌های Vazirmatn و vendorهای Alpine/HTMX از مبدأ کپی شدند.
- **به‌روزرسانی‌ی باگ‌های مبدأ در هنگام پورت:**
  - B6/B13/B14/B15/B19 در `storefront` اصلاح شدند (هیچ‌کدام از کدهای باگ‌دار مبدأ کپی نشد).
  - باگ جدیدی که در پورت پیدا شد: `OrderItem.save()` مقدار `unit_price` را با فرمول تک‌محوره‌ی قدیمی بازنویسی می‌کند. checkout از flag داخلی `_skip_price_calc` استفاده می‌کند چون قیمت از `storefront.pricing` (منبع واحد) در سبد محاسبه شده.
  - `size` آیتم سفارش در checkout از ابعاد سفارشی پر می‌شود تا `generate_tasks()` اختلاف ابعاد را به قطعات BOM اعمال کند.

### تست‌ها
- `storefront`: ۱۷ تست، OK.
- `product`: ۱۰۶ تست، OK (دو اجرای پی‌درهمی).
- smoke test کل مسیر (افزودن با ابعاد سفارشی → تسویه → ساخت سفارش با قیمت/اندازه/رنگ صحیح → نمایش سفارش) اجرا و تأیید شد؛ پس از تأیید حذف شد.

### هنوز باقی (فاز ۵–۷)
- ~~`payments` (با اصلاح B7–B10)~~ → **انجام شد**.
- ~~`discounts`~~ → **انجام شد**.
- ~~`accounts` بدون تعویض `AUTH_USER_MODEL`~~ → **انجام شد** (از `UserProfile` یک‌تایی استفاده می‌کند).
- ~~`storefront/admin.py` برای مدل‌های فروشگاه~~ → **انجام شد**.
- ~~صفحه‌ی پرداخت و مرجوعی در UI~~ → **انجام شد** (payments + returns templates).
- ~~اپ `api` (فاز ۸)~~ → **انجام شد** (DRF + JWT + drf_spectacular).

### فاز ۵–۷ (انجام‌شده در ۱۴۰۵/۰۷/۱۵)
- **اپ `payments`** ساخته شد: `Payment` (یک‌تایی سفارش) + `Transaction` + دو درگاه `ZarinpalGateway` و `CashOnDeliveryGateway` + `PaymentGatewayFactory`.
  - `payment_create` مبلغ را از `order.final_amount` می‌گیرد و verify با `order.final_amount` مقابله می‌کند (اصلاح B7–B10).
  - پس از verify موفق، `Order` به `paid` تغییر وضعیت می‌یابد و `paid_at` ثبت می‌شود.
- **اپ `discounts`** ساخته شد: `Discount` (کد/نوع/مقدار/محدوده/کاربرد) + فرم‌ها + ویوهای لیست/ایجاد/ویرایش/اعمال/حذف.
  - سبد (`Cart.discount`) و سفارش (`Order.discount`) هر دو به `Discount` وابسته شدند.
  - `Cart.discount_amount` و `Cart.final_price` محاسبه می‌شوند و در checkout به `Order` منتقل می‌شوند.
- **اپ `accounts`** بدون تغییر `AUTH_USER_MODEL` ساخته شد: ورود/ثبت‌نام/پروفایل/بازیابی رمز عبور + مدل `UserProfile` یک‌تایی برای اطلاعات تکمیلی.
- **`storefront/admin.py`** برای `ColorOption`, `ProductImage`, `ProductReview`, `ComparisonList`, `Address`, `ReturnRequest`.
- **UI مرجوعی**: ویوهای `ReturnRequestListView/CreateView/DetailView` + قالب‌های `returns/`.
- **UI پرداخت**: قالب‌های `payments/payment_create.html` و `payments/payment_error.html`.
- مسیرها در `selvi/urls.py` زیر `/accounts/`، `/discounts/` و `/payments/` ثبت شدند.

### smoke test فاز ۵–۷
- **پرداخت:** افزودن به سبد → تسویه → ساخت سفارش → پرداخت COD → verify → سفارش به `paid` تغییر وضعیت داد و `Payment` به `success` تغییر یافت. ✅
- **تخفیف:** اعمال کد `SAVE10` (۱۰٪) → `Cart.discount_amount = 200` و `final_price = 1800` → سفارش ثبت‌شده `discount_id = 1`، `discount_amount = 200`، `final_amount = 1800` را دارد. ✅
- `manage.py check`: بدون خطا. ✅

### فاز ۸ (انجام‌شده در ۱۴۰۵/۰۷/۱۶)
- **اپ `api`** با DRF پورت شد: `ProductViewSet`، `ProductCategoryViewSet`، `CartViewSet`، `OrderViewSet`، `DiscountViewSet`، `ProductionTaskViewSet` + `CurrentUserView` + JWT auth (`TokenObtainPairView`/`TokenRefreshView`) + `drf_spectacular` schema.
- `INSTALLED_APPS` به `api`، `rest_framework`، `rest_framework_simplejwt`، `drf_spectacular` گسترش یافت.
- `REST_FRAMEWORK` و `SIMPLE_JWT` در `selvi/settings.py` پیکربندی شدند.
- مسیرها در `selvi/urls.py` زیر `/api/v1/` ثبت شدند.
- `api/signals.py` برای ساخت خودکار `Cart` در هنگام ثبت‌نام کاربر (از `Cart.objects.get_or_create`).
- smoke test: `/api/v1/products/` (۴ محصول)، `/api/v1/categories/` (۴ دسته)، `/api/v1/cart/` (سبد خالی)، `/api/v1/auth/users/me/` (پروفایل کاربر)، `/api/v1/auth/token/` (JWT pair) — همه ۲۰۰. ✅

### نکات پایانی
- `Product.image` و `ProductCategory.image` فیلدهای `ImageField` دارند و از طریق ادمین `product` قابل آپلود هستند.
- برای لوگو و عکس کاور صفحه home، مدل `SiteSettings` (singleton) در `storefront` ساخته شد تا ادمین بتواند از طریق ادمین Django آن را تغییر دهد.
- همه مسیرهای ورود/خروج/ثبت‌نام/بازیابی رمز عبور به namespace `accounts:` تغییر یافتند (`product/templates/base.html`، `base_shop.html`، `registration/login.html`، `storefront/includes/header.html`).
- `manage.py check` بدون خطا. ✅
