/* الگوهای مشترک UI بخش برش — یک بار تعریف، همه‌جا بازاستفاده:
 * ۱) ردیف بازشونده جدول (data-row-expand + aria-controls)
 * ۲) کلیک روی کل ردیف دارای data-href (بدون تداخل با کنترل‌های داخلی)
 */
(() => {
  document.addEventListener('click', event => {
    const toggle = event.target.closest('[data-row-expand]');
    if (toggle) {
      const details = document.getElementById(toggle.getAttribute('aria-controls') || '');
      if (details) {
        const open = details.hasAttribute('hidden');
        if (open) details.removeAttribute('hidden');
        else details.setAttribute('hidden', '');
        toggle.setAttribute('aria-expanded', String(open));
      }
      return;
    }
    if (event.target.closest('tr[data-href]') &&
        !event.target.closest('a, button, input, select, label, details, summary, form')) {
      window.location.href = event.target.closest('tr[data-href]').dataset.href;
    }
  });
})();
