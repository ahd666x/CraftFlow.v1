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
    document.getElementById('sticky-selection-count').textContent = `${fmt(data.selected_order_count)} سفارش انتخاب شده`;
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
      ? `سیاست مصرف: ${data.strategy} · تخمین چیدمان ردیفی`
      : '';

    data.materials.forEach(item => {
      const card = document.createElement('div');
      card.className = `preview-material ${item.fits ? 'good' : 'bad'}`;
      const badge = item.fits
        ? '<span class="badge text-bg-success">چیدمان شدنی</span>'
        : '<span class="badge text-bg-danger">کمبود/عدم تطبیق</span>';
      const estimate = item.estimated_sheets === null ? 'نامشخص' : fmt(item.estimated_sheets);
      const basis = item.estimate_is_exact ? 'بر پایه چیدمان الگوریتم' : 'تقریبی';
      const extra = item.estimated_additional_sheets === null ? '—' : fmt(item.estimated_additional_sheets);
      card.innerHTML = `<div class="preview-material-heading"><b>${esc(item.name)} · ${esc(item.thickness)} mm</b><span>${fmt(item.piece_count)} قطعه</span>${badge}</div>
        <details class="mt-2"><summary>جزئیات تخمینی</summary>
          <div class="small mt-2">مساحت لازم: ${fmt(item.required_area_m2)} m²</div>
          <div class="small">ورق تخمینی: <b>${estimate}</b> <span class="text-muted">(${basis})</span></div>
          <div class="small">آزاد: ${fmt(item.available_sheets)} (کامل ${fmt(item.available_full_sheets)} · پرت ${fmt(item.available_offcut_sheets)}) · مساحت آزاد ${fmt(item.available_area_m2)} m²</div>
          <div class="small">کسری تخمینی: ${extra} ورق</div>
        </details>`;
      holder.appendChild(card);
    });

    const shortages = data.materials.filter(material => !material.fits);
    const blocked = data.materials.length === 0 || shortages.length > 0;
    submit.disabled = blocked;
    if (!data.materials.length) {
      verdict.className = 'preview-verdict neutral mb-3';
      verdict.textContent = 'سفارشی انتخاب نشده';
    } else if (shortages.length) {
      const names = shortages.map(item => `${item.name} ${item.thickness}mm`).join('، ');
      verdict.className = 'preview-verdict bad mb-3';
      verdict.innerHTML = `✘ موجودی کافی نیست: ${esc(names)} <a href="${form.dataset.stockUrl}">ثبت موجودی</a>`;
    } else {
      verdict.className = 'preview-verdict good mb-3';
      verdict.textContent = '✔ قابل برش با موجودی آزاد';
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
        document.getElementById('preview-state').textContent = 'برای محاسبه سفارش انتخاب کنید';
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

  document.addEventListener('ws:selection-changed', updatePreview);
  document.addEventListener('ws:specs-changed', updatePreview);
  stockPolicies.forEach(input => input.addEventListener('change', updatePreview));
  kerf.addEventListener('input', updatePreview);
  planningModes.forEach(input => input.addEventListener('change', updatePreview));
  form.addEventListener('submit', () => {
    submit.disabled = true;
    submit.querySelector('.spinner-border').hidden = false;
    submit.querySelector('.submit-label').textContent = 'در حال محاسبه…';
  });
  updatePreview();
})();
