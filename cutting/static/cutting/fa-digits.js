/* ماژول مشترک پذیرش ارقام فارسی/عربی در فیلدهای number (بازبینی دور ۳، بند ۳ CNC).
 * همین کد چهار بار کپی شده بود (app.js، workspace.js، planner.js، sheet_plans.html)
 * و نسخهٔ planner.js هنوز setRangeText می‌زد — که روی type=number خطای
 * InvalidStateError می‌دهد و preventDefault قبلاً رقم را می‌بلعید.
 * یک منبع واحد با گارد رونویسی (window.__faDigitsBound)؛ درج با چسباندن رشته. */
(() => {
  'use strict';
  if (window.__faDigitsBound) return;
  window.__faDigitsBound = true;
  const FA_DIGITS = '۰۱۲۳۴۵۶۷۸۹', AR_DIGITS = '٠١٢٣٤٥٦٧٨٩';
  document.addEventListener('keydown', (e) => {
    const target = e.target;
    if (!target || target.type !== 'number' || e.ctrlKey || e.metaKey || e.altKey) return;
    let digit = FA_DIGITS.indexOf(e.key);
    if (digit < 0) digit = AR_DIGITS.indexOf(e.key);
    if (digit < 0) return;
    e.preventDefault();
    // ورودی type=number از API انتخاب (setRangeText/selectionStart) پشتیبانی
    // نمی‌کند و InvalidStateError می‌دهد — درج با چسباندن رشته انجام می‌شود.
    let start = null, end = null;
    try { start = target.selectionStart; end = target.selectionEnd; } catch (_) { /* برخی موتورها پرتاب می‌کنند */ }
    start = start ?? target.value.length;
    end = end ?? target.value.length;
    target.value = target.value.slice(0, start) + String(digit) + target.value.slice(end);
    target.dispatchEvent(new Event('input', { bubbles: true }));
  });
})();
