/* تنظیمات برش + پیش‌نمایش نیاز مواد. صف سفارش‌ها و جدول قطعات به فضای کاری
 * (workspace.js) منتقل شده‌اند؛ این اسکریپت انتخاب سفارش‌ها را از hidden
 * inputs فرم می‌خواند و با رویدادهای ws:selection-changed / ws:specs-changed
 * پیش‌نمایش را تازه می‌کند. رفتار پیش‌نمایش و قفل دکمهٔ «محاسبه و رزرو»
 * دست‌نخورده مانده است.
 */
(() => {
  const form = document.getElementById('planning-form');
  if (!form) return;

  const kerf = document.getElementById('kerf');
  const planningModes = [...document.querySelectorAll('input[name="planning_mode"]')];
  const stockPolicies = [...document.querySelectorAll('input[name="stock_policy"]')];
  const cncUseOffcuts = document.querySelector('input[name="cnc_use_offcuts"]');
  const submit = document.getElementById('plan-submit');
  let timer = null;
  let controller = null;

  const fmt = value => Number(value || 0).toLocaleString('en-US');
  const esc = value => String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
  const selected = () =>
    [...form.querySelectorAll('input[name="order_ids"]')].map(input => input.value);

  function setCounts(data) {
    document.getElementById('selected-count').textContent = fmt(data.selected_order_count);
    document.getElementById('sticky-selection-count').textContent = `${fmt(data.selected_order_count)} سفارش`;
    document.getElementById('selected-pieces').textContent = fmt(data.piece_count);
  }

  function paint(data) {
    setCounts(data);
    const holder = document.getElementById('preview-materials');
    const empty = document.getElementById('preview-empty');
    const error = document.getElementById('preview-error');
    const verdict = document.getElementById('preview-verdict');
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
        ? '<span class="badge text-bg-success" title="چیدمان شدنی">آماده</span>'
        : '<span class="badge text-bg-danger" title="کمبود موجودی یا عدم تطبیق">کمبود</span>';
      const estimate = item.estimated_sheets === null ? 'نامشخص' : fmt(item.estimated_sheets);
      const basis = item.estimate_is_exact ? 'بر پایه چیدمان الگوریتم' : 'تقریبی';
      const extra = item.estimated_additional_sheets === null ? '—' : fmt(item.estimated_additional_sheets);
      card.innerHTML = `<div class="preview-material-heading"><b>${esc(item.name)} · ${esc(item.thickness)}</b><span class="num">${fmt(item.piece_count)} قطعه</span>${badge}</div>
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
    if (!data.materials.length) {
      verdict.className = 'preview-verdict neutral';
      verdict.textContent = 'سفارشی انتخاب نشده';
    } else if (shortages.length) {
      const names = shortages.map(item => `${item.name} ${item.thickness}mm`).join('، ');
      verdict.className = 'preview-verdict bad';
      verdict.innerHTML = `✘ کمبود: ${esc(names)} <a href="${form.dataset.stockUrl}">ثبت موجودی</a>`;
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
        document.getElementById('preview-state').textContent = 'سفارش انتخاب کنید';
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
      }
    }, 600);
  }

  /* خلاصهٔ یک‌خطی تنظیمات کنار دکمهٔ کشو + aria-expanded کشوها (نمایش‌محور) */
  function paintSettingsBrief() {
    const brief = document.getElementById('settings-brief');
    if (!brief) return;
    const policy = stockPolicies.find(input => input.checked);
    const policyLabel = policy ? (policy.closest('label')?.querySelector('.form-check-label')?.textContent || '').trim() : '';
    brief.textContent = ` — تیغه ${kerf.value || '—'}${policyLabel ? ` · ${policyLabel}` : ''}`;
  }
  document.querySelectorAll('[data-bs-toggle="offcanvas"]').forEach(button => {
    const target = document.querySelector(button.dataset.bsTarget || '');
    if (!target) return;
    target.addEventListener('show.bs.offcanvas', () => button.setAttribute('aria-expanded', 'true'));
    target.addEventListener('hidden.bs.offcanvas', () => button.setAttribute('aria-expanded', 'false'));
  });

  document.addEventListener('ws:selection-changed', updatePreview);
  document.addEventListener('ws:specs-changed', updatePreview);
  stockPolicies.forEach(input => input.addEventListener('change', () => { updatePreview(); paintSettingsBrief(); }));
  kerf.addEventListener('input', () => { updatePreview(); paintSettingsBrief(); });
  planningModes.forEach(input => input.addEventListener('change', updatePreview));
  form.addEventListener('submit', () => {
    submit.disabled = true;
    submit.querySelector('.spinner-border').hidden = false;
    submit.querySelector('.submit-label').textContent = 'در حال محاسبه…';
  });
  updatePreview();
  paintSettingsBrief();
})();
