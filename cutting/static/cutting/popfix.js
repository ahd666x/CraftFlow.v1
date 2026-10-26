/* ============================================================
   popfix.js — بازبینی دور ۳: پنل‌های <details class="detail-pop">
   position:absolute هستند و داخل ظرف‌های اسکرول‌دار (‎.table-responsive‎،
   ‎.page-head‎، ‎.offcanvas-body‎) بریده می‌شوند. وقتی جد / گسترهٔ بازکردن
   ظرفی کلیپ‌کننده بود، پنل به position:fixed می‌رود و روی خود عنصر خلاصه
   لنگر می‌شود؛ با اسکرول/تغییر اندازه پنل بسته می‌شود (تا جا باز نماند).
   هیچ‌کجا به DOM خاصی وابسته نیست — هر detail-pop کلیپ‌شده‌ای را درست می‌کند.
   ============================================================ */
(() => {
  'use strict';
  const CLIP = /(auto|scroll|clip|hidden)/;

  function clippedAncestor(el) {
    let n = el.parentElement;
    while (n && n !== document.documentElement) {
      const s = getComputedStyle(n);
      if (CLIP.test(s.overflowX + ' ' + s.overflowY)) return n;
      n = n.parentElement;
    }
    return null;
  }

  function panelOf(d) { return d.querySelector(':scope > .detail-pop-panel'); }

  function place(d) {
    const panel = panelOf(d);
    if (!panel) return;
    const r = d.getBoundingClientRect();
    const rtl = getComputedStyle(panel).direction === 'rtl';
    panel.classList.add('pop-escaped');
    panel.style.top = (r.bottom + 6) + 'px';
    panel.style.insetInlineStart = (rtl
      ? Math.max(8, window.innerWidth - r.right)
      : Math.max(8, r.left)) + 'px';
    panel.style.maxHeight = Math.max(160, window.innerHeight - r.bottom - 24) + 'px';
    panel.style.overflowY = 'auto';
  }

  function clear(d) {
    const panel = panelOf(d);
    if (!panel) return;
    panel.classList.remove('pop-escaped');
    panel.style.top = '';
    panel.style.insetInlineStart = '';
    panel.style.maxHeight = '';
    panel.style.overflowY = '';
  }

  function closeEscaped() {
    document.querySelectorAll('details.detail-pop[open]').forEach((d) => {
      const panel = panelOf(d);
      if (panel && panel.classList.contains('pop-escaped')) d.open = false;
    });
  }

  document.addEventListener('toggle', (e) => {
    const d = e.target;
    if (!(d instanceof HTMLDetailsElement) || !d.classList.contains('detail-pop')) return;
    if (d.open && clippedAncestor(d)) place(d);
    else clear(d);
  }, true);

  // پنل fixed با اسکرول صفحه جا می‌ماند → بستن تمیزتر از جابه‌جایی زنده است
  window.addEventListener('scroll', closeEscaped, true);
  window.addEventListener('resize', closeEscaped);
})();
