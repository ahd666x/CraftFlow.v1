# نقشه راه یکپارچه: یکپارچه‌سازی و رفع باگ‌های اپ‌های cutting و cnc_resizer

## خلاصه وضعیت
دو تحلیل انجام شده که مکمل هم‌اند:
1. **تحلیل فیچر/روال کاری** — قابلیت‌های گم شده یا ناقص
2. **تحلیل باگ‌های UI/تجربه** — موارد خراب/ناسازگار که مانع کار اپراتورند

---

## فاز ۱: استقرار کارکرد پایه (P0 بحرانی) — هفته ۱
**هدف:** اپراتور بتواند کار روزمره را بدون بلوکر انجام دهد

### ۱.۱ Fix بلوکرهای فوری (از تحلیل باگ‌ها)
| Task | فایل | توضیح |
|------|------|-------|
| Fix ذخیره قالب | `cnc_resizer/static/cnc_resizer/history.js:143` | چک `payload.edits.length` به جای `payload.kind === 'none'` |
| Fix انتخاب المان داخلی | `cnc_resizer/static/cnc_resizer/selection.js:55-59`, `edit-tools.js:649-656` | `child` pathها (سوراخ/شیار داخلی) باید قطعه را انتخاب کنند |
| اجباری کردن ورق در (0,0) | `cnc_resizer/static/cnc_resizer/viewport.js:310-315` | `sheet_bbox_finished` → الزامی `x_min=0, y_min=0` |
| Warningهای نام فایل → Info | `cnc_resizer/engine/parts.py:541-551`, `app.js:64-72` | تبدیل `insp-warn` به `insp-info` برای چک‌های نام فایل |

### ۱.۲ پیش‌نمایش G-code ورق چندقطعه‌ای (از تحلیل فیچر P0)
| Task | فایل | توضیح |
|------|------|-------|
| API پیش‌نمایش بصری | `cnc_resizer/views.py` + `sheet_adapter.py` | Endpoint جدید برای SVG/Canvas مسیرهای ابزار کل ورق |
| UI در sheet_plans | `cnc_resizer/templates/cnc_resizer/sheet_plans.html` | دکمه/مودال "پیش‌نمایش مسیرها" با هایلایت تصادف، nhanh، تعویض ابزار |
| تداخل با گته دیالکت | `cnc_resizer/sheet_adapter.py` | استفاده از همان `build_sheet_program` برای پیش‌نمایش صادقانه |

---

## فاز ۲: حلقه‌های حیاتی (P1 مهم) — هفته ۲
**هدف:** بستن حلقه‌های بازِ روال کاری (Manual Queue، Re-nest، درگ/کشش روان)

### ۲.۱ صف دستی و Re-nest (از تحلیل فیچر P1)
| Task | فایل | توضیح |
|------|------|-------|
| صفحه `/cnc/manual/` | `cnc_resizer/views.py` + Template جدید | لیست تمام `CncPlanningIssue` با `reason_code=manual_required` + لینک مستقیم به `/cnc/?task_id=` |
| اکشن Re-nest Sheet | `cnc_resizer/views.py` + `sheet_nesting.py` | دکمه روی `CncSheetPlan` برای اجرای مجدد nesting با همان قطعات + موجودی فعلی |
| سیگنال قالب قطعه → Sheet | `cnc_resizer/part_templates.py` + Signal | وقتی `CncPartTemplate` آپدیت شد، `CncSheetPlan`های تحت تأثیر فلگ شوند |

### ۲.۲ بهبود تعاملی نقشه (از تحلیل باگ P1)
| Task | فایل | توضیح |
|------|------|-------|
| نوار کشش همیشه قابل مشاهده | `cnc_resizer/static/cnc_resizer/edit-tools.js:737-791` | عدم محو شدن با از بین رفتن انتخاب؛ sticky به آخرین قطعه |
| درگ از روی child operations | `cnc_resizer/static/cnc_resizer/viewport.js:354-368` | Hit area برای `child` pathها → انتخاب قطعه مالک |
| کاهش Debounce پیش‌نمایش | `cnc_resizer/static/cnc_resizer/edit-tools.js:57` | 260ms → 150ms + immediate preview در درگ |
| انیمیشن/Transition برای panels | `cnc_resizer/static/cnc_resizer/map.css` + `app.js:235-250` | 150ms ease برای باز/بسته، hover states |

---

## فاز ۳: پایه‌های داده و UI سیستماتیک (P2) — هفته ۳
**هدف:** مقیاس‌پذیری، یکپارچگی داده، UI حرفه‌ای

### ۳.۱ یکپارچگی داده (از تحلیل فیچر P2)
| Task | فایل | توضیح |
|------|------|-------|
| داشبورد پرت یکپارچه | `cutting/views.py` + Template جدید | ادغام `cutting:inventory/offcuts` + پرت‌های `created_from_cnc_plan` |
| اعتبارسنجی CNC در planner | `cutting/views.py:planner_preview` | فراخوانی `analyze_piece` برای هر قطعه در حالت CNC mode |
| رزرو موجودی بهینه | `cutting/views.py:_reserved_counts` + `cnc_resizer/sheet_nesting.py:_cut_reserved_counts` | Advisory lock یا جدول رزرو واحد |

### ۳.۲ UI حرفه‌ای مینیمال (از تحلیل باگ P2)
| Task | فایل | توضیح |
|------|------|-------|
| رنگ‌بندی Colorblind-safe | `cnc_resizer/static/cnc_resizer/map.css:14-16`, `viewport.js:13-15` | ۱۰ رنگ با کنتراست AAA روی بوم روشن/تیره |
| سیستم Alert/Toast بازنویسی | `cnc_resizer/static/cnc_resizer/app.js:18-42`, `map.css:222-230` | Toast‌های موقت، grouping، auto-dismiss، position غیرتداخلی |
| تایپوگرافی سیستم | `map.css:26-30` | Scale یکپارچه (12/13.5/15px)، line-height 1.6، font-variant-numeric |
| Micro-interactions | `map.css`全域 | Hover/focus/active states، transition 150ms، ripple روی دکمه‌ها |
| پنل‌های Stack management | `app.js:235-250` | فقط یک پنل باز، ESC برای بستن، focus trap |

---

## فاز ۴: بهینه‌سازی و پلیس (P3-P4) — هفته ۴+
| Task | اولویت | توضیح |
|------|--------|-------|
| Touch-friendly sheet map | P3 | Pinch-zoom، touch targets ۴۴px، gesture درگ/زوم |
| Part template → Sheet sync | P3 | Signal روی `CncPartTemplate.save` → flag affected `CncSheetPlan` |
| Preflight report در sheet_plans | P3 | فیلد `preflight_report` JSONField روی `CncSheetPlan` |
| بهینه‌سازی کوئری‌های N+1 | P4 | `select_related`/`prefetch_related` در views پرتردد |
| تست‌های یکپارچگی (Integration tests) | P4 | Coverage برای سناریوهای Cut→CNC→Manual→Re-nest |

---

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

---

## معیارهای قبول (Definition of Done) برای هر فاز

### فاز ۱ Done khi:
- [ ] اپراتور می‌تواند قالب ذخیره کند بدون خطا
- [ ] کلیک روی سوراخ/شیار داخلی قطعه، قطعه را انتخاب می‌کند
- [ ] ورق در نمای نقشه همیشه در (0,0) است
- [ ] هشدارهای نام فایل به عنوان Info (آبی) نه Warning (زرد) نمایش داده می‌شوند
- [ ] در `sheet_plans` می‌توان G-code ترکیبی ورق را پیش‌نمایش کرد (SVG مسیرها + تصادف‌ها)

### فاز ۲ Done khi:
- [ ] صفحه `/cnc/manual/` همه Issueهای manual_required را نشان می‌دهد
- [ ] دکمه Re-nest روی هر `CncSheetPlan` کار می‌کند
- [ ] درگ/کشش روی نقشه روان است (پیش‌نمایش < 200ms)
- [ ] نوار کشش همیشه برای قطعه فعال قابل مشاهده است

### فاز ۳ Done khi:
- [ ] داشبورد پرت واحد تمام بقايا (Cut + CNC) را نمایش می‌دهد
- [ ] Planner در حالت CNC فایل‌ها را اعتبارسنجی می‌کند قبل از برنامه‌ریزی
- [ ] سیستم رنگ/تایپوگرافی/Alert یکپارچه و حرفه‌ای است
- [ ] پنل‌ها تداخل ندارند، انیمیشن‌های روان دارند

---

## نکات اجرایی مهم

1. **تست در محیط واقعی:** هر تغییر در `edit-tools.js`/`selection.js`/`viewport.js` باید با فایل‌های CNC واقعی (t.16، پنل خم‌کاری، پرونده‌های.Panel) تست شود
2. **Fail-closed حفظ شود:** هیچ تغییر در UI نباید گته‌های سرور (collision، dialect، geometry) را دور بزند
3. **Backward compatibility:** APIهای موجود (`engine_preview`، `engine_output`، `parts_api`) نباید breaking change داشته باشند
4. **Performance budget:** رندر نقشه < 100ms برای ۵۰ قطعه، پیش‌نمایش سرور < 500ms

---

## فایل‌های کلیدی برای مراجعه سریع

| حوزه | فایل‌ها |
|-------|---------|
| **Core CNC Map** | `cnc_resizer/static/cnc_resizer/{app,selection,edit-tools,viewport,map.css}.js` |
| **History/Templates** | `cnc_resizer/static/cnc_resizer/history.js` |
| **Sheet Nesting** | `cnc_resizer/sheet_nesting.py`, `cnc_resizer/sheet_adapter.py` |
| **Part Detection** | `cnc_resizer/engine/parts.py`, `cnc_resizer/gcode_sheet.py` |
| **Part Templates** | `cnc_resizer/part_templates.py`, `cnc_resizer/models.py` (CncPartTemplate) |
| **Cutting Planner** | `cutting/views.py`, `cutting/models.py`, `cutting/templates/cutting/{planner,plan_detail}.html` |
| **Sheet Plans UI** | `cnc_resizer/templates/cnc_resizer/sheet_plans.html`, `cnc_resizer/views.py:sheet_plans` |
| **Services/Resolution** | `cnc_resizer/services.py`, `cnc_resizer/controller.py` |

---

## وضعیت فعلی (۲۰۲۶-۱۰-۲۴)
- **انجام شده:** تحلیل کامل دو اپ، شناسایی ۱۷+ باگ/خلأ، اولویت‌بندی
- **در tiến trình:** هیچ‌کدام (آماده برای شروع فاز ۱.۱)
- **بلوکرها:** هیچ‌کدام فنی نیستند — فقط تصمیم اجرا

---

## برای ایجنت جدید (اگر چت از دست برود)

1. این نقشه را بخوانید و **فاز ۱.۱** را از Task ۱.۱.۱ آغاز کنید
2. فایل‌های کلیدی بالا را مطالعه کنید تا کدبیس را درک کنید
3. تمام تغییرات باید **fail-closed** حفظ کنند — گته‌های سرور مقدس‌اند
4. تست با فایل‌های واقعی در `cnc_resizer/testdata/` ضروری است
5. در صورت سوال: `kilo_memory_recall mode=search query="roadmap_cnc_cutting_integration"`