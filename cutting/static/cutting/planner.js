/* پیش‌نمایش نیاز مواد v2 — کارت جدا نیست؛ verdict در نوار پایین ثابت و
 * جزئیات هر متریال در پنل بالاآمدهٔ «پیش‌نمایش نیاز مواد». انتخاب سفارش‌ها از
 * hidden inputs فرم با رویدادهای ws:selection-changed / ws:specs-changed تازه
 * می‌شود. رفتار پیش‌نمایش و قفل دکمهٔ «محاسبه و رزرو» دست‌نخورده مانده است.
 */
(() => {
  const form = document.getElementById('planning-form');
  if (!form) return;

  const kerf = document.getElementById('kerf');
  const planningModes = [...document.querySelectorAll('input[name="planning_mode"]')];
  const stockPolicies = [...document.querySelectorAll('input[name="stock_policy"]')];
  const cncUseOffcuts = document.querySelector('input[name="cnc_use_offcuts"]');
  const submit = document.getElementById('plan-submit');
  const verdict = document.getElementById('preview-verdict');
  const toggle = document.getElementById('preview-toggle');
  const pop = document.getElementById('preview-pop');
  let timer = null;
  let controller = null;

  const fmt = value => Number(value || 0).toLocaleString('en-US');
  const esc = value => String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
  const selected = () =>
    [...form.querySelectorAll('input[name="order_ids"]')].map(input => input.value);

  function setCounts(data) {
    document.getElementById('sticky-selection-count').textContent =
      `${fmt(data.selected_order_count)} سفارش · ${fmt(data.piece_count)} قطعه`;
  }

  function paint(data) {
    setCounts(data);
    const holder = document.getElementById('preview-materials');
    const empty = document.getElementById('preview-empty');
    const error = document.getElementById('preview-error');
    holder.replaceChildren();
    error.hidden = true;
    empty.hidden = data.materials.length > 0;
    document.getElementById('preview-strategy').textContent = data.strategy
      ? `سیاست: ${data.strategy}`
      : '';

    data.materials.forEach(item => {
      const card = document.createElement('div');
      card.className = `preview-material ${item.fits ? 'good' : 'bad'}`;
      const badge = item.fits
        ? '<span class="badge-status is-ok" title="چیدمان شدنی">آماده</span>'
        : '<span class="badge-status is-bad" title="کمبود موجودی یا عدم تطبیق">کمبود</span>';
      const estimate = item.estimated_sheets === null ? 'نامشخص' : fmt(item.estimated_sheets);
      const basis = item.estimate_is_exact ? 'بر پایه چیدمان الگوریتم' : 'تقریبی';
      const extra = item.estimated_additional_sheets === null ? '—' : fmt(item.estimated_additional_sheets);
      card.innerHTML = `<div class="preview-material-heading"><b>${esc(item.name)}</b><span class="num">${fmt(item.piece_count)} قطعه</span>${badge}</div>
        <details class="pm-details"><summary>جزئیات</summary>
          <div class="small mt-2">مساحت لازم: ${fmt(item.required_area_m2)} m²</div>
          <div class="small">ورق تخمینی: <b>${estimate}</b> <span class="text-muted">(${basis})</span></div>
          <div class="small">آزاد: ${fmt(item.available_sheets)} (کامل ${fmt(item.available_full_sheets)} · پرت ${fmt(item.available_offcut_sheets)}) · ${fmt(item.available_area_m2)} m²</div>
          <div class="small">کسری تخمینی: ${extra} ورق</div>
        </details>`;
      holder.appendChild(card);
    });

    const shortages = data.materials.filter(material => !material.fits);
    const blocked = data.materials.length === 0 || shortages.length > 0;
    submit.disabled = blocked;
    /* فاز ۳.۱.۲: اعتبارسنجی فایل‌های CNC — هشدار است نه بلوکر؛ سرور همان‌ها را
       در POST واقعی به صف دستی می‌برد (fail-closed دست‌نخورده). */
    const issues = Array.isArray(data.cnc_issues) ? data.cnc_issues : [];
    const issueBox = document.getElementById('preview-cnc-issues');
    const cncModeActive = planningModes.find(input => input.checked)?.value === 'cnc_sheets';
    if (issueBox) {
      if (issues.length && cncModeActive) {
        const rows = issues.slice(0, 5).map(issue =>
          `<li><b>${esc(issue.part_name)}</b> <span class="num">(${esc(issue.pieces)} عدد)</span> — ${esc(issue.message)}</li>`
        ).join('');
        const more = issues.length > 5
          ? `<li class="text-muted">و ${issues.length - 5} قطعهٔ دیگر…</li>` : '';
        issueBox.innerHTML = `<details open><summary><span class="badge-status is-warn">اعتبارسنجی CNC</span>
          ${issues.length} قطعه نیازمند اصلاح/الگو است — در «محاسبه و رزرو» به صف دستی می‌رود</summary>
          <ul class="small mt-1 mb-0 ps-3">${rows}${more}</ul></details>`;
        issueBox.hidden = false;
      } else {
        issueBox.hidden = true;
        issueBox.innerHTML = '';
      }
    }
    if (!data.materials.length) {
      verdict.className = 'preview-verdict neutral';
      verdict.textContent = 'سفارشی انتخاب نشده';
    } else if (shortages.length) {
      const names = shortages.map(item => item.name).join('، ');
      verdict.className = 'preview-verdict bad';
      const stockLink = form.dataset.stockUrl ? ` <a href="${form.dataset.stockUrl}">ثبت موجودی</a>` : '';
      verdict.innerHTML = `✘ کمبود: ${esc(names)}${stockLink}`;
    } else if (issues.length) {
      verdict.className = 'preview-verdict good';
      verdict.innerHTML = `✔ قابل برش — <span class="text-muted">${issues.length} قطعه به صف دستی CNC می‌رود</span>`;
    } else {
      verdict.className = 'preview-verdict good';
      verdict.textContent = '✔ قابل برش';
    }
  }

  function updatePreview() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const ids = selected();
      const error = document.getElementById('preview-error');
      error.hidden = true;
      submit.disabled = true;
      if (!ids.length) {
        paint({ selected_order_count: 0, piece_count: 0, materials: [] });
        document.getElementById('preview-state').textContent = '';
        return;
      }
      if (controller) controller.abort();
      controller = new AbortController();
      document.getElementById('preview-state').textContent = 'در حال محاسبه…';
      const params = new URLSearchParams();
      ids.forEach(id => params.append('order_ids', id));
      if (kerf.value) params.set('kerf', kerf.value);
      const selectedMode = planningModes.find(input => input.checked);
      if (selectedMode) params.set('planning_mode', selectedMode.value);
      const policy = stockPolicies.find(input => input.checked);
      if (policy) params.set('stock_policy', policy.value);
      if (selectedMode && selectedMode.value === 'cnc_sheets' && cncUseOffcuts && cncUseOffcuts.checked) {
        params.set('cnc_use_offcuts', '1');
      }
      try {
        const response = await fetch(`${form.dataset.previewUrl}?${params}`, {
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          signal: controller.signal
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'پیش‌نمایش در دسترس نیست.');
        paint(data);
        document.getElementById('preview-state').textContent = 'به‌روز شد';
      } catch (error) {
        if (error.name === 'AbortError') return;
        submit.disabled = true;
        document.getElementById('preview-error').textContent = error.message;
        document.getElementById('preview-error').hidden = false;
        document.getElementById('preview-state').textContent = 'خطا';
        // علتِ غیرفعال‌بودن دکمه باید بیرونِ پنلِ بسته هم دیده شود (بازبینی UI/UX بند ۸)
        verdict.className = 'preview-verdict bad';
        // بازبینی UI/UX بند ۱۳: با خطای پیش‌نمایش، راه «تلاش دوباره» باید باشد
        verdict.innerHTML = `✘ ${esc(error.message)} — <a href="#" id="preview-retry">تلاش دوباره</a>`;
      }
    }, 600);
  }

  /* پنل بالاآمدهٔ پیش‌نمایش — فقط با کلیک، بیرون از آن بسته می‌شود */
  function setPop(open) {
    if (!pop || !toggle) return;
    pop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }
  toggle?.addEventListener('click', () => setPop(pop.hidden));
  document.addEventListener('click', event => {
    if (!pop || pop.hidden) return;
    if (event.target.closest('#preview-pop') || event.target.closest('#preview-toggle')) return;
    setPop(false);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && pop && !pop.hidden) setPop(false);
  });

  /* aria-expanded کشوهای کناری سینک می‌ماند */
  document.querySelectorAll('[data-bs-toggle="offcanvas"]').forEach(button => {
    const target = document.querySelector(button.dataset.bsTarget || '');
    if (!target) return;
    target.addEventListener('show.bs.offcanvas', () => button.setAttribute('aria-expanded', 'true'));
    target.addEventListener('hidden.bs.offcanvas', () => button.setAttribute('aria-expanded', 'false'));
  });

  document.addEventListener('ws:selection-changed', updatePreview);
  document.addEventListener('ws:specs-changed', updatePreview);
  stockPolicies.forEach(input => input.addEventListener('change', updatePreview));
  kerf.addEventListener('input', updatePreview);
  planningModes.forEach(input => input.addEventListener('change', updatePreview));
  cncUseOffcuts?.addEventListener('change', updatePreview);

  /* بازبینی UI/UX بند ۱۳: کلیک روی «تلاش دوباره» = اجرای دوبارهٔ پیش‌نمایش */
  verdict.addEventListener('click', event => {
    if (event.target.id === 'preview-retry') {
      event.preventDefault();
      updatePreview();
    }
  });

  /* بازبینی UI/UX بند ۴: فالبک مرورگرهای بدون :has() برای نمایش چک‌باکس «پرت»
     در حالت CNC — کلاس روی page-head همان کار :has() را می‌کند. */
  const pageHead = document.querySelector('.planner-page .page-head');
  function syncCncModeClass() {
    if (!pageHead) return;
    const isCnc = planningModes.find(input => input.checked)?.value === 'cnc_sheets';
    pageHead.classList.toggle('is-cnc-mode', Boolean(isCnc));
  }
  planningModes.forEach(input => input.addEventListener('change', syncCncModeClass));
  syncCncModeClass();
  form.addEventListener('submit', () => {
    submit.disabled = true;
    submit.querySelector('.spinner-border').hidden = false;
    submit.querySelector('.submit-label').textContent = 'در حال محاسبه…';
  });
  updatePreview();

  /* خلاصهٔ تنظیمات برش کنار دکمهٔ «محاسبه و رزرو» (بازبینی UI/UX بند ۷) —
     تنظیمات در کشو است و کاربر هنگام محاسبه نمی‌بیند چه انتخاب شده. */
  const settingsSummary = document.getElementById('settings-summary');
  const planningForm = document.getElementById('planning-form');
  function updateSettingsSummary() {
    if (!settingsSummary || !planningForm) return;
    const kerf = planningForm.querySelector('input[name="kerf"]')?.value ?? '؟';
    const trim = planningForm.querySelector('input[name="trim"]')?.value ?? '0';
    const policy = planningForm.querySelector('input[name="stock_policy"]:checked');
    const policyLabel = (policy?.closest('label')?.textContent || '').trim() || '—';
    const engines = [...planningForm.querySelectorAll('input[name="algorithms"]:checked')]
      .map(input => (input.closest('label')?.textContent || '').trim())
      .filter(Boolean).map(text => text.replace(/\s*\(.*\)$/, '')).join(' / ');
    const grain = planningForm.querySelector('input[name="respect_grain"]:checked') ? ' · رگه' : '';
    settingsSummary.textContent = `تیغه ${kerf} · ${engines || '—'} · ${policyLabel} · Trim ${trim}${grain}`;
  }
  updateSettingsSummary();
  planningForm?.addEventListener('change', updateSettingsSummary);

  /* پذیرش ارقام فارسی/عربی در فیلدهای عددی (بازبینی UI/UX بند ۱۸) —
     کلید رقم فارسی معادل رقم لاتین در همان محل درج می‌شود. */
  const FA_DIGITS = '۰۱۲۳۴۵۶۷۸۹', AR_DIGITS = '٠١٢٣٤٥٦٧٨٩';
  document.addEventListener('keydown', (e) => {
    const target = e.target;
    if (!target || target.type !== 'number' || e.ctrlKey || e.metaKey) return;
    let digit = FA_DIGITS.indexOf(e.key);
    if (digit < 0) digit = AR_DIGITS.indexOf(e.key);
    if (digit < 0) return;
    e.preventDefault();
    const start = target.selectionStart ?? target.value.length;
    const end = target.selectionEnd ?? target.value.length;
    target.setRangeText(String(digit), start, end, 'end');
    target.dispatchEvent(new Event('input', { bubbles: true }));
  });
})();
