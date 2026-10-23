# فضای کاری برش (`/cutting/`) — معماری و قواعد

این سند خلاصهٔ پیاده‌سازی «فضای کاری برش» است: تبدیل صفحهٔ `/cutting/` از صف سادهٔ سفارش به محیط یکپارچهٔ «سفارش ← آیتم ← متریال ← جدول قطعات قابل‌ویرایش» که برنامهٔ برش (فایل `.CUT`) و چیدمان CNC هر دو از **یک نسخهٔ مؤثر مشخصات قطعه** تغذیه می‌شوند.

## ۱. مدل داده

- **`TaskSpecOverride`** (`cutting.models`) — یک‌به‌یک با `ProductionTask` (ایستگاه برش). هیچ‌گاه `Part` یا `ProductionTask` را نمی‌نویسد؛ همهٔ ویرایش‌های اپراتور در همین overlay ثبت می‌شود.
  - فیلدها: `length`, `width`, `quantity`, `material`, `turn`, `edge_f4/f5/f18/f26`, `excluded`, `excluded_reason`.
  - `None` روی هر فیلد یعنی «از رکورد پایه ارث ببر»؛ رشتهٔ خالی روی نوار لبه یعنی «صریحاً خالی شود».
  - `version` با هر ویرایش موفق یکی زیاد می‌شود و برای قفل خوش‌بینانه (optimistic concurrency) استفاده می‌شود؛ ردیف بدون overlay یعنی version صفر.
  - اگر overlay از همه فیلدها خالی شود، رکورد حذف می‌شود (بازگشت کامل به مشخصات پایه).
- **`TaskSpecOverrideEvent`** — رد توان از هر تغییر فیلد (قدیم/جدید/علت/کاربر).
- **`CutPlanTask.spec_hash`** (migration 0012) — اثر انگشت مشخصات مؤثر در لحظهٔ برنامه‌ریزی.

## ۲. resolver مشخصات مؤثر (`cutting/specs.py`)

نقطهٔ واحد خواندن مشخصات برای همهٔ مصرف‌کنندگان برنامه‌ریزی:

- `effective_spec(task, override)` / `effective_specs(tasks)` — نسخهٔ دسته‌ای بدون N+1 (۴ کوئری ثابت: overrideها + برنامه‌های برش planned + چیدمان‌های CNC planned + issueهای manual باز).
- `quantity_remaining = max(0, تعداد مؤثر − انجام‌شده − رزرو برش planned − رزرو CNC planned)`؛ قطعهٔ `excluded` همیشه صفر.
- `spec_hash(spec)` — SHA-256 روی `(length,width,material,turn,quantity_total,excluded)`.
- `status_code`: `ready` / `excluded` / `needs_cnc` / `needs_cnc_manual` / `locked_in_plan` (`auto_adjusted` و `manual_resolved` در گزارش برنامه‌ریزی CNC تولید می‌شوند).
- `plan_lock(task)` — اگر تسک در برنامهٔ برش یا چیدمان CNC با وضعیت planned باشد، **هر** ویرایش/حذف/بازنشانی با خطای «ابتدا برنامهٔ #N را لغو کنید» رد می‌شود (همان قاعدهٔ قفل موجودی انبار). این قاعده مسئلهٔ «خروجی قدیمی» را ریشه‌ای می‌بندد.

### قواعد اعتبارسنجی ویرایش (`validate_override`)
- ابعاد: عددی، بزرگ‌تر از صفر، حداکثر یک رقم اعشار.
- `quantity`: کف = `completed_quantity + رزرو فعال`، سقف ۱۰۰۰۰.
- قطعهٔ CNC-first:
  - تغییر **طول** مجاز است و از مسیر `resolve_cnc_file(effective_length=…)` اعمال می‌شود؛
  - تغییر **عرض** در override ثبت می‌شود ولی برنامه‌ریزی خودکار آن را به صف دستی می‌فرستد (`width_changed`)؛
  - تغییر **متریال** فقط با ضخامت برابر؛ در غیر این صورت رد (`ضخامت برابر`).
- مقدارِ برابرِ مقدار پایه → فیلد به حالت ارث برمی‌گردد (reset-to-inherit)؛ تکرار مقدار فعلی → no-op و بدون bump نسخه.
- ویرایش ابعاد هرگز `excluded` را پاک نمی‌کند (دو تصمیم مستقل).

## ۳. اتصال موتورهای برنامه‌ریزی

| مصرف‌کننده | رفتار جدید |
|---|---|
| `cutting.views.planner` (POST) | ابعاد/تعداد/متریال/چرخش از `effective_specs`؛ `excluded` حذف؛ override تعداد می‌تواند تسک را به برنامه برگرداند (`quantity > completed`)؛ گروه‌بندی با `spec.material_id`؛ `spec_hash` روی `CutPlanTask` ذخیره می‌شود؛ نوار لبه روی قطعات `.CUT` سوار می‌شود |
| `cutting.views.planner_preview` | همان ابعاد مؤثر (بدون رزرو) |
| `cnc_resizer.sheet_nesting.create_cnc_sheet_plans` | `excluded` حذف؛ pending از تعداد مؤثر؛ هر قطعهٔ layout `spec_hash` دارد |
| `cnc_resizer.anomaly_report.find_unusual_parts` | ابعاد/چرخش/متریال مؤثر (override طولِ غیرمعمول هم گزارش می‌شود) |

### گیت‌های staleness (fail-closed)
- `mark_cut`، `download_cut` (فایل `.CUT`)، `complete_sheet_plan` (حالت «مصرف شد») و `download_sheet_plan`: اگر `spec_hash` ذخیره‌شده با مشخصات مؤثر فعلی نخواند → مسدود با پیام «مشخصات قطعه پس از برنامه‌ریزی تغییر کرده است؛ برنامه را لغو و دوباره بسازید.»
- برنامه‌های قدیمی بدون hash → رفتار قبلی.
- این گیت‌ها حتی تغییر بیرونی `Part` (از ادمین) را هم می‌گیرند، چون hash روی مشخصات **مؤثر** است.

## ۴. اصلاح خودکار CNC و صف دستی

`plan_cnc_with_report` (به‌جای دوتاییِ قدیمی که حفظ شده) برای هر تسک یکی از این وضعیت‌ها را برمی‌گرداند:

| وضعیت | معنا |
|---|---|
| `nested` | طول مؤثر = طول پایه؛ فایل پایه بدون تغییر استفاده شد |
| `auto_adjusted` | فایل مشتق با الگوی تأییدشده ساخته شد (اپراتور همان تغییر را دستی تکرار نمی‌کند) |
| `manual_resolved` | فایل دستیِ ثبت‌شدهٔ اپراتور برای این طول استفاده شد |
| `manual_required` | به صف دستی رفت با `reason_code`: `no_profile` / `profile_failed` / `width_changed` / `thickness_changed` |

قرارداد بنیادی: **فایل پایه هرگز بی‌صدا برای طول متفاوت تحویل نمی‌شود.** `resolve_cnc_file(part, effective_length=…)` اول دنبال فایل دستی ثبت‌شده برای همان (قطعه، طول) می‌گردد، بعد الگوی تأییدشده، و در نبود هر دو خطای `CncResolutionError` می‌دهد که به `manual_required` تبدیل می‌شود.

### ثبت فایل دستی — `POST /cnc/api/manual-resolution/`
بدنه: `{"task_id": …, "content": "<متن G-code>"}`. گیت‌ها:
1. تأیید دیالکت Workshop (`controller.verify_program`)؛
2. طول اندازه‌گیری‌شدهٔ Y باید با طول مؤثر قطعه `±0.05mm` بخواند؛
3. ثبت به‌صورت `CncFile(kind=derived, params.mode='manual')` با کلید content-addressed روی (نسخهٔ فایل پایه، قطعه، طول)؛
4. issueهای `manual_required` بازِ همان تسک خودکار `resolved` می‌شوند.

در صفحهٔ برنامه‌های CNC، issueهای `manual_required` با علت فارسی و دکمهٔ «رفع در ابزار CNC» (`/cnc/?task_id=…`) نمایش داده می‌شوند.

## ۵. API فضای کاری (JSON، همه با دسترسی `cutting_operator`)

| مسیر | کار |
|---|---|
| `GET /cutting/workspace/orders/` | صف سفارش‌ها (جست‌وجو، وضعیت، صفحه‌بندی، `planning_state`) |
| `GET /cutting/workspace/orders/<id>/` | آیتم‌ها + چیپ متریال‌ها |
| `GET /cutting/workspace/orders/<id>/parts/?item=&material=&q=&sort=&page=` | ردیف‌های جدول + جمع نیاز هر متریال |
| `PATCH|POST /cutting/workspace/tasks/<id>/spec/` | `{version, fields:{…}, reason?}`؛ ناسازگاری نسخه → **409** با ردیف تازه |
| `POST /cutting/workspace/tasks/<id>/exclude/` | `{version, reason}` — علت الزامی |
| `POST /cutting/workspace/tasks/<id>/include/` | بازگرداندن قطعهٔ خارج‌شده |
| `POST /cutting/workspace/tasks/<id>/reset/` | پاک‌کردن همهٔ overrideها (با event) |

همهٔ عملیات داخل `transaction.atomic` با `select_for_update` روی ردیف override اجرا می‌شوند و خطاها با پیام فارسی (۴۰۰/۴۰۹) برمی‌گردند.

## ۶. رابط کاربری

- `planner.html` میزبان فضای کاری است؛ کارت تنظیمات برش، `planning_mode`، پیش‌نمایش مواد و نوار «محاسبه و رزرو» حفظ شده‌اند و انتخاب سفارش‌ها با hidden inputs `order_ids` به همان POST قدیمی می‌رسد.
- `workspace.js`: صف سفارش‌ها (کلیک = فوکوس؛ چک‌باکس = انتخاب برای برنامه‌ریزی)، چیپ آیتم/متریال («همهٔ آیتم‌ها» و «همهٔ متریال‌ها» پیش‌فرض و با کلیک مجدد بازمی‌گردند)، جدول قطعات با ویرایش سلولی (blur/Enter)، نشانگر ذخیره/خطا، برچسب «● تغییرکرده»، ردیف‌های قفل غیرفعال با لینک برنامه، مودال «خارج از برنامه» با علت الزامی (Escape/کلیک بیرون)، مرتب‌سازی ستونی و جست‌وجوی سریع نام/بارکد.
- `planner.js` با رویدادهای `ws:selection-changed` و `ws:specs-changed` پیش‌نمایش موجودی را بلافاصله تازه می‌کند (ویرایش تعداد/حذف قطعه در پیش‌نمایش و verdict «موجودی کافی» منعکس می‌شود).

## ۷. دامنهٔ عمداً بیرون از محدوده

- بازسازی خودکار چیدمان پس از تغییر (اپراتور برنامه را لغو و دوباره می‌سازد)؛
- نوشتن روی `Part` پایه یا اعمال override روی ایستگاه‌های بعدی/بارکد؛
- ویرایش نام قطعه (روی بارکد/`.CUT` می‌رود و فقط‌خواندنی است)؛
- اثر نوار لبه بر منطق برنامه‌ریزی (فقط ذخیره/نمایش/XML/override).
