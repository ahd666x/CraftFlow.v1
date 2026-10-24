# نقشه راه یکپارچه: یکپارچه‌سازی و رفع باگ‌های اپ‌های cutting و cnc_resizer

> این سند نسخهٔ دائمیِ داخل مخزن است (تا در فشرده‌شدن context چت از دست نرود — درس Task 59).
> تاریخ سند اصلی: ۲۰۲۶-۱۰-۲۴ · افزودن «وضعیت اجرا» در ۲۰۲۶-۱۰-۲۵.

## خلاصه وضعیت
دو تحلیل مکمل پایهٔ این نقشه بود:
1. **تحلیل فیچر/روال کاری** — قابلیت‌های گم‌شده یا ناقص
2. **تحلیل باگ‌های UI/تجربه** — موارد خراب/ناسازگار که مانع کار اپراتورند

---

## فاز ۱: استقرار کارکرد پایه (P0 بحرانی)

### ۱.۱ رفع بلوکرهای فوری
| Task | فایل | توضیح |
|------|------|-------|
| Fix ذخیره قالب | `cnc_resizer/static/cnc_resizer/history.js` | چک `payload.edits.length` به جای `payload.kind === 'none'` |
| انتخاب المان داخلی | `selection.js`, `edit-tools.js` | مسیرهای child (سوراخ/شیار داخلی) قطعه را انتخاب کنند |
| اجباری کردن ورق در (0,0) | `viewport.js` | `sheet_bbox_finished` → الزامی `x_min=0, y_min=0` |
| Warningهای نام فایل → Info | `engine/parts.py`, `app.js` | تبدیل `insp-warn` به `insp-info` برای چک‌های نام فایل |

### ۱.۲ پیش‌نمایش G-code ورق چندقطعه‌ای
| Task | فایل | توضیح |
|------|------|-------|
| API پیش‌نمایش بصری | `views.py` + `sheet_preview.py` | Endpoint مسیرهای ابزار کل ورق از روی gcode ذخیره‌شده (پارسر قفل‌بردیالکت، fail-closed) |
| UI در sheet_plans | `sheet_plans.html` + `sheet_preview.js` | دکمه/مودال «مسیرها» با رنگ ابزار، سریع خط‌چین، ریسک قرمز، آمار |
| تداخل با گته دیالکت | `sheet_adapter.py` | پیش‌نمایش فقط از خروجی همان `build_sheet_program` — پیش‌نمایش صادقانه |

## فاز ۲: حلقه‌های حیاتی (P1 مهم)

### ۲.۱ صف دستی و Re-nest
| Task | فایل | توضیح |
|------|------|-------|
| صفحه `/cnc/manual/` | `manual_queue.py` + `views.py` + قالب جدید | لیست Issueهای manual_required + سیگنال‌های فقط-خواندنی + لینک عمیق به ابزار CAD |
| اکشن Re-nest Sheet | `views.py:manual_renest` | بازچینش با همان موتور `plan_cnc_with_report`؛ موفقیت → رفع issue با ممیزی؛ شکست → fail-closed |
| سیگنال قالب قطعه | `manual_queue.py` (فاز ۲.۲: `part_template_names` + پنل قالب‌ها در history CAD) | حلقهٔ قالب پارامتریک ↔ صف دستی |

### ۲.۲ بهبود تعاملی نقشه
| Task | فایل | توضیح |
|------|------|-------|
| نوار کشش همیشه قابل مشاهده | `edit-tools.js` | sticky به آخرین قطعه |
| درگ از روی child operations | `viewport.js` | Hit area برای مسیرهای child |
| کاهش Debounce پیش‌نمایش | `edit-tools.js` | 260ms → 150ms |
| انیمیشن/Transition پنل‌ها | `map.css` + `app.js` | 150ms ease برای باز/بسته |

## فاز ۳: پایه‌های داده و UI سیستماتیک (P2)

### ۳.۱ یکپارچگی داده
| Task | فایل | توضیح |
|------|------|-------|
| داشبورد پرت یکپارچه | `cutting/views.py` | ادغام پرت‌های CUT + CNC با منشأ |
| اعتبارسنجی CNC در planner | `sheet_nesting.py:preflight_cnc_pieces` | پیش‌بررسی فقط-خواندنی قطعات در پیش‌نمایش planner (`cnc_issues` additive) |
| رزرو موجودی بهینه | `cutting/views.py` + `sheet_nesting.py` | تجمیع SQL رزروها + رفع بن‌بست قفل‌ها |

### ۳.۲ UI حرفه‌ای مینیمال
| Task | فایل | توضیح |
|------|------|-------|
| رنگ‌بندی Colorblind-safe | `map.css` + `viewport.js`/`app.js`/`sheet_preview.js` | پالت ۱۰ رنگ با کنتراست ≥4.5:1 و حذف جفت‌های هم‌ارز |
| سیستم Alert/Toast | `app.js` + `map.css` | گروه‌بندی با key، ttl اختیاری برای پیام‌های موفقیت، انیمیشن ورود/خروج |
| تایپوگرافی سیستم | `map.css` | توکن‌های `--fs-12/--fs-13/--fs-15`، line-height 1.6، ارقام جدولی |
| Micro-interactions | `map.css` + `app.js` | transition 150ms، حالت فشرده، ripple لمسی (با reduced-motion خاموش) |
| پنل‌های Stack management | `app.js` | تک‌پنل باز، ESC برای بستن + بازگشت فوکوس (بدون تلهٔ سخت — پنل‌ها غیرمودال‌اند) |

## فاز ۴: بهینه‌سازی و پلیس (P3-P4)
| Task | اولویت | توضیح |
|------|--------|-------|
| Touch-friendly sheet map | P3 | پینچ‌زوم/درگ لمسی و اهداف ۴۴px (از قبل در viewport.js/map.css برقرار بود) |
| Part template → Sheet sync | P3 | سیگنال `post_save/post_delete` روی `CncPartTemplate` → فلگ مشورتی `template_stale` روی `CncSheetPlan`های planned |
| Preflight report در sheet_plans | P3 | فیلد `preflight_report` JSONField روی `CncSheetPlan` + بج «پیش‌بررسی: سالم/خطا/—» |
| بهینه‌سازی کوئری‌های N+1 | P4 | ممیزی شد: مسیرهای داغ (`sheet_plans`/`_layout_preview`/workspace) خالص JSON یا `select_related` — N+1 واقعی یافت نشد |
| تست‌های یکپارچگی | P4 | `test_integration_flow.py`: صف دستی→فایل دستی→بازچینش→قطعهٔ چیده‌شده + چرخهٔ کامل فلگ قالب |

## وابستگی‌های فنی (Dependency Graph)

```
Phase 1.1 (Fixes) ──────┐
                        ├──► Phase 1.2 (Sheet G-code Preview)
Phase 1.1 (Fixes) ──────┘

Phase 1.2 ──────────────┐
                        ├──► Phase 2.1 (Manual Queue + Re-nest)
Phase 2.2 (Map UX) ─────┘

Phase 2.1 ──────────────┐
                        ├──► Phase 3.1 (Unified Offcuts + Planner Validation)
Phase 2.2 ──────────────┘
                        ├──► Phase 3.2 (Design System + Alerts)
Phase 3.1 ──────────────┘
Phase 3.2 ──────────────┘
                        ├──► Phase 4 (Polish + Touch + Tests)
```

## نکات اجرایی مهم

1. **تست در محیط واقعی:** تغییرهای `edit-tools.js`/`selection.js`/`viewport.js` با فایل‌های CNC واقعی (t.16، پنل خم‌کاری، .Panel) آزمون شده‌اند (آزمون زنده با agent-browser در Taskهای 57 و 60).
2. **Fail-closed حفظ شود:** هیچ تغییری در UI گته‌های سرور (collision، dialect، geometry، spec_hash) را دور نمی‌زند — همهٔ افزوده‌ها additive و فقط-خواندنی/مشورتی‌اند.
3. **Backward compatibility:** APIهای موجود (`engine_preview`، `engine_output`، `parts_api`) breaking change نداشته‌اند؛ کلیدهای جدید (infos، cnc_issues، part_template_names، preflight_report، template_stale_info) همگی additive.
4. **Performance budget:** پارس پیش‌نمایش ورق ~0.01s برای ۳۷KB؛ رندر نقشه و پیش‌نمایش‌ها در بودجهٔ تعیین‌شده.

## فایل‌های کلیدی برای مراجعه سریع

| حوزه | فایل‌ها |
|-------|---------|
| **Core CNC Map** | `cnc_resizer/static/cnc_resizer/{app,selection,edit-tools,viewport,compare,map.css}` |
| **History/Templates** | `history.js` + `part_templates.py` + `signals.py` |
| **Sheet Nesting** | `sheet_nesting.py`, `sheet_adapter.py`, `sheet_preview.py` |
| **Part Detection** | `engine/parts.py`, `gcode_sheet.py` |
| **Cutting Planner** | `cutting/views.py`, `cutting/models.py`, `cutting/templates/cutting/*` |
| **Sheet Plans UI** | `templates/cnc_resizer/sheet_plans.html`, `views.py:sheet_plans` |
| **Manual Queue** | `manual_queue.py`, `views.py:manual_queue/manual_renest`, `templates/cnc_resizer/manual_queue.html` |

---

## وضعیت اجرا (به‌روزرسانی ۲۰۲۶-۱۰-۲۵)

| فاز | وضعیت | کامیت‌ها / Task worklog |
|-----|-------|------------------------|
| ۱.۱ بلوکرهای P0 | ✅ | `4e4eaed` · Task 56 (۱.۱.۲ از قبل اعمال بود) |
| ۱.۲ پیش‌نمایش G-code ورق | ✅ | `e8144f3` · Task 57 |
| ۲.۱ صف دستی + Re-nest | ✅ | `5b334b0` · Task 58 |
| ۲.۲ حلقهٔ قالب قطعه + map-UX | ✅ | `367e562` + بقایا در `cdef0b9` · Task 58/59 |
| ۳.۱ یکپارچگی داده | ✅ | `cdef0b9` · Task 59 |
| ۳.۲ مقایسهٔ قبل/بعد + tolerance پرؤ runtime | ✅ | `767d3c9`, `193ec91` · Task 60 |
| ۳.۲ مکمل: پالت/توضست/پنل/میکرو/تایپوگرافی | ✅ | `bb5b93f` · Task 61 |
| ۴ P3: preflight_report + template→sheet sync | ✅ | `912996c` · Task 61 |
| ۴ P3: Touch (پینچ/درگ/۴۴px) | ✅ از قبل برقرار | viewport.js (تأیید در Task 61) |
| ۴ P4: N+1 | ✅ ممیزی — N+1 واقعی یافت نشد | Task 61 |
| ۴ P4: تست‌های یکپارچگی | ✅ | `test_integration_flow.py` · ۶۵۰/۶۵۰ سبز |

معیارهای قبول هر سه فاز ۱–۳ برقرار است؛ فاز ۴ نیز بسته شد. ادامهٔ کار پس از این نقطه = نگهداری/بازخورد اپراتور و موارد کشف‌شدهٔ جدید.
