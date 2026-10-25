/* سلوی — دیالوگ‌های درون‌برنامه‌ای (بند ب-۸ بازبینی UI/UX)
 * جایگزین window.confirm / window.prompt: مودال RTL هم‌سبک با هر دو صفحهٔ
 * بوت‌استرپی (ایستگاه برش/صف CNC) و بوم CAD (map.css). بدون وابستگی —
 * استایل خودحامل با متغیرهای اختیاری تا تم هر صفحه اعمال شود.
 *
 * API:
 *   SelviDialog.confirm({ title, message, okLabel, cancelLabel, danger })  -> Promise<boolean>
 *   SelviDialog.prompt({ title, message, value, placeholder, okLabel,
 *                        cancelLabel, inputType, ltr })                    -> Promise<string|null>
 *
 * فرم‌های دارای data-confirm="…" (و اختیاری data-confirm-danger) به‌صورت
 * سراسری رهگیری می‌شوند: submit متوقف، مودال نمایش، در تأیید requestSubmit.
 * قلاب سراسری در فاز capture است تا listenerهای confirm قدیمی دوبار نپرسند.
 */
(() => {
  'use strict';
  if (window.SelviDialog) return;

  const TXT = { ok: 'تأیید', cancel: 'انصراف' };
  let root = null;
  let dlg = null;
  let els = null;
  let active = null; // { resolve, kind, prev }

  const STYLE = `
  .svdlg-overlay{position:fixed;inset:0;z-index:9990;display:grid;place-items:center;
    background:rgba(15,23,42,.38);padding:16px;direction:rtl;
    animation:svdlg-fade .14s ease}
  .svdlg{background:var(--svdlg-surface,#fff);border:1px solid var(--svdlg-line,#d8dee7);
    border-radius:14px;box-shadow:0 12px 40px rgba(15,23,42,.24);width:min(430px,94vw);
    padding:16px 18px;color:var(--svdlg-text,#17222e);
    font:13.5px/1.75 "Vazirmatn","Segoe UI",Tahoma,system-ui,sans-serif;
    animation:svdlg-pop .16s ease}
  .svdlg-title{display:block;font-size:14px;font-weight:800;margin:0 0 6px}
  .svdlg-msg{margin:0 0 12px;white-space:pre-line;overflow-wrap:anywhere}
  .svdlg-msg:empty{display:none}
  .svdlg-input{width:100%;border:1px solid var(--svdlg-line,#d8dee7);border-radius:8px;
    padding:7px 10px;font:inherit;color:inherit;background:var(--svdlg-surface-2,#f8fafc);
    margin:0 0 12px;min-height:34px}
  .svdlg-input:focus{outline:2px solid var(--svdlg-accent,#2563eb);outline-offset:1px}
  .svdlg-btns{display:flex;gap:8px}
  .svdlg-btns button{min-height:34px;padding:5px 18px;border-radius:8px;cursor:pointer;
    border:1px solid var(--svdlg-line,#d8dee7);background:var(--svdlg-surface,#fff);
    color:inherit;font:inherit}
  .svdlg-btns button:focus-visible{outline:2px solid var(--svdlg-accent,#2563eb);outline-offset:1px}
  .svdlg-ok{background:var(--svdlg-accent,#2563eb);border-color:var(--svdlg-accent,#2563eb);
    color:#fff;font-weight:700}
  .svdlg-ok.danger{background:var(--svdlg-danger,#b3261e);border-color:var(--svdlg-danger,#b3261e)}
  .svdlg-ok:hover{filter:brightness(1.06)}
  .svdlg-cancel:hover{background:var(--svdlg-surface-2,#f2f4f8)}
  @keyframes svdlg-fade{from{opacity:0}}
  @keyframes svdlg-pop{from{opacity:0;transform:translateY(6px) scale(.985)}}
  @media (prefers-reduced-motion:reduce){
    .svdlg-overlay,.svdlg{animation:none !important;transition:none !important}
  }`;

  function ensureDom() {
    if (root) return;
    root = document.createElement('div');
    root.className = 'svdlg-overlay';
    root.hidden = true;
    root.innerHTML =
      '<style>' + STYLE + '</style>' +
      '<div class="svdlg" role="dialog" aria-modal="true" aria-labelledby="svdlg-title-el">' +
      '  <b class="svdlg-title" id="svdlg-title-el"></b>' +
      '  <p class="svdlg-msg"></p>' +
      '  <input class="svdlg-input" type="text" hidden>' +
      '  <div class="svdlg-btns">' +
      '    <button type="button" class="svdlg-cancel"></button>' +
      '    <button type="button" class="svdlg-ok"></button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(root);
    dlg = root.querySelector('.svdlg');
    els = {
      title: root.querySelector('.svdlg-title'),
      msg: root.querySelector('.svdlg-msg'),
      input: root.querySelector('.svdlg-input'),
      cancel: root.querySelector('.svdlg-cancel'),
      ok: root.querySelector('.svdlg-ok'),
    };

    // کلیدها داخل مودال به بیرون نشت نکند (میانبرهای صفحهٔ CAD/برش خاموش بمانند)
    root.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Escape') { e.preventDefault(); finish(active && active.kind === 'prompt' ? null : false); }
      else if (e.key === 'Enter' && active && active.kind === 'prompt' && e.target === els.input) { e.preventDefault(); els.ok.click(); }
    });
    root.addEventListener('keyup', (e) => e.stopPropagation());
    // کلیک روی پس‌زمینه = انصراف (گزینهٔ امن)
    root.addEventListener('mousedown', (e) => { if (e.target === root) finish(active && active.kind === 'prompt' ? null : false); });
    els.cancel.addEventListener('click', () => finish(active && active.kind === 'prompt' ? null : false));
    els.ok.addEventListener('click', () => finish(active && active.kind === 'prompt' ? String(els.input.value) : true));
  }

  function finish(result) {
    if (!active) return;
    const cur = active;
    active = null;
    root.hidden = true;
    if (cur.prev && typeof cur.prev.focus === 'function') { try { cur.prev.focus(); } catch (_) {} }
    cur.resolve(result);
  }

  function open(opts, kind) {
    ensureDom();
    if (active) finish(kind === 'prompt' ? null : false); // مودال قبلی لغو
    const o = opts || {};
    els.title.textContent = o.title || '';
    els.title.hidden = !o.title;
    els.msg.textContent = o.message || '';
    const isPrompt = kind === 'prompt';
    els.input.hidden = !isPrompt;
    if (isPrompt) {
      els.input.type = o.inputType === 'number' ? 'number' : 'text';
      if (o.inputType === 'decimal') { els.input.type = 'text'; els.input.inputMode = 'decimal'; }
      else { els.input.inputMode = ''; }
      els.input.setAttribute('dir', o.ltr === false ? 'rtl' : 'ltr');
      els.input.placeholder = o.placeholder || '';
      els.input.value = o.value != null ? String(o.value) : '';
    }
    els.ok.textContent = o.okLabel || TXT.ok;
    els.ok.classList.toggle('danger', !!o.danger);
    els.cancel.textContent = o.cancelLabel || TXT.cancel;
    dlg.setAttribute('aria-labelledby', o.title ? 'svdlg-title-el' : '');
    return new Promise((resolve) => {
      active = { resolve, kind, prev: document.activeElement };
      root.hidden = false;
      if (isPrompt) { els.input.focus(); try { els.input.select(); } catch (_) {} }
      else els.ok.focus();
    });
  }

  function confirm(opts) { return open(opts, 'confirm').then((r) => r === true); }
  function prompt(opts) { return open(opts, 'prompt'); }

  // ── فرم‌های [data-confirm] (جایگزین onsubmit="return confirm(…)") ──
  document.addEventListener('submit', (e) => {
    const form = e.target instanceof Element ? e.target : null;
    if (!form || !form.hasAttribute('data-confirm')) return;
    if (form.dataset.confirmArmed === '1') return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const go = () => {
      form.dataset.confirmArmed = '1';
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else form.submit();
    };
    confirm({
      message: form.getAttribute('data-confirm'),
      title: form.getAttribute('data-confirm-title') || '',
      danger: form.hasAttribute('data-confirm-danger'),
    }).then((ok) => { if (ok) go(); });
  }, true);

  window.SelviDialog = { confirm, prompt };
})();
