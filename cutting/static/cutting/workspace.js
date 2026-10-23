/* فضای کاری برش — صف سفارش‌ها ← آیتم ← متریال ← جدول قطعات قابل‌ویرایش.
 * همهٔ داده‌ها از API فضای کاری (cutting.workspace_views) می‌آید؛ فیلترها و
 * ویرایش‌های سلولی بدون بارگذاری کامل صفحه انجام می‌شوند. اعداد لاتین‌اند و
 * پیام‌ها فارسی. Fail-closed: خطای هر عملیات در همان ردیف/نوار وضعیت نشان
 * داده می‌شود و مقدار سلول به حالت قبل برمی‌گردد.
 */
(() => {
  const root = document.getElementById('workspace-root');
  const configEl = document.getElementById('ws-config');
  if (!root || !configEl) return;

  const CONFIG = JSON.parse(configEl.textContent);
  const MATERIALS = JSON.parse(document.getElementById('ws-materials').textContent);
  const MATERIAL_BY_ID = Object.fromEntries(MATERIALS.map(m => [String(m.id), m]));
  const CSRF = (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || '';
  const form = document.getElementById('planning-form');

  const fmt = value => Number(value || 0).toLocaleString('en-US');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
  const pattern = (url, id) => String(url).replace('/0/', `/${id}/`);
  const EDGE_LABELS = { f4: 'نوار طول ۱', f5: 'نوار طول ۲', f18: 'نوار عرض ۱', f26: 'نوار عرض ۲' };

  const state = {
    queueQuery: '', queueStatus: '', queuePage: 1, queuePages: 1,
    selection: new Set(),
    focusId: null, orderInfo: null, items: [], materials: [],
    item: 'all', material: 'all', search: '', sort: '',
    rows: [], partsPage: 1, partsPages: 1,
  };

  async function api(url, options = {}) {
    const response = await fetch(url, {
      headers: {
        'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      },
      ...options,
    });
    let data = {};
    try { data = await response.json(); } catch (error) { /* non-JSON */ }
    if (!response.ok) {
      const error = new Error(data.error || 'درخواست انجام نشد.');
      error.payload = data;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function announce(message) {
    const live = document.getElementById('ws-live');
    if (live) live.textContent = message;
  }

  /* ---------- skeleton ---------- */

  root.innerHTML = `
    <section class="card" aria-label="صف سفارش‌ها">
      <div class="panel-toolbar">
        <div class="flex-fill" style="min-width:12rem">
          <label class="form-label" for="ws-order-search">جست‌وجوی شماره سفارش یا مشتری</label>
          <input id="ws-order-search" class="form-control form-control-sm" placeholder="مثلاً 142 یا نام مشتری" autocomplete="off">
        </div>
        <div style="min-width:9rem">
          <label class="form-label" for="ws-order-status">وضعیت سفارش</label>
          <select id="ws-order-status" class="form-select form-select-sm"><option value="">همه وضعیت‌ها</option></select>
        </div>
      </div>
      <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2 border-0 py-2">
        <b class="panel-title">تقاضای برش — صف سفارش‌ها</b>
        <span id="ws-order-count" class="small text-muted" aria-live="polite"></span>
      </div>
      <div class="table-responsive"><table class="table table-hover align-middle ws-low-density">
        <thead><tr>
          <th></th><th>سفارش / مشتری</th><th class="d-none d-lg-table-cell">نماینده</th>
          <th class="d-none d-md-table-cell">تاریخ</th><th class="d-none d-md-table-cell">آیتم</th>
          <th>قطعه باقی‌مانده</th><th>وضعیت برنامه</th>
        </tr></thead>
        <tbody id="ws-order-rows"></tbody>
      </table></div>
      <div class="card-footer d-flex justify-content-between align-items-center py-1">
        <button id="ws-queue-prev" type="button" class="btn btn-sm btn-outline-secondary">قبلی</button>
        <span id="ws-queue-page" class="small text-muted num"></span>
        <button id="ws-queue-next" type="button" class="btn btn-sm btn-outline-secondary">بعدی</button>
      </div>
    </section>

    <section class="card mt-3" id="ws-parts-card" hidden aria-label="جدول قطعات">
      <div class="card-header d-flex flex-wrap align-items-center gap-2 py-2 border-0">
        <b class="panel-title" id="ws-focus-title"></b>
        <span class="small text-muted">آیتم‌ها:</span>
        <div id="ws-item-chips" class="ws-chips"></div>
        <span class="small text-muted">متریال‌ها:</span>
        <div id="ws-material-chips" class="ws-chips"></div>
        <input id="ws-parts-search" class="form-control form-control-sm" style="max-width:13rem" placeholder="نام قطعه یا بارکد…" autocomplete="off">
      </div>
      <div class="table-responsive"><table class="table table-sm table-hover align-middle ws-low-density ws-edit-grid">
        <thead><tr>
          <th>قطعه</th><th class="d-none d-lg-table-cell">بارکد</th>
          <th data-sort="length" class="ws-sortable">طول</th><th data-sort="width" class="ws-sortable">عرض</th>
          <th data-sort="quantity" class="ws-sortable">تعداد</th>
          <th data-sort="material" class="ws-sortable">متریال</th><th>چرخش</th>
          <th class="d-none d-xl-table-cell">نوار لبه (طول۱/طول۲/عرض۱/عرض۲)</th>
          <th data-sort="status" class="ws-sortable">وضعیت</th><th></th>
        </tr></thead>
        <tbody id="ws-part-rows"></tbody>
      </table></div>
      <div class="card-footer d-flex flex-wrap justify-content-between align-items-center gap-2 py-1">
        <span id="ws-summary" class="small text-muted"></span>
        <span class="d-flex align-items-center gap-2">
          <button id="ws-parts-prev" type="button" class="btn btn-sm btn-outline-secondary">قبلی</button>
          <span id="ws-parts-page" class="small text-muted num"></span>
          <button id="ws-parts-next" type="button" class="btn btn-sm btn-outline-secondary">بعدی</button>
        </span>
      </div>
    </section>
    <div id="ws-modal-holder"></div>
    <p id="ws-live" class="visually-hidden" aria-live="polite"></p>
  `;

  /* ---------- order queue ---------- */

  async function loadQueue() {
    const params = new URLSearchParams({ page: state.queuePage });
    if (state.queueQuery) params.set('q', state.queueQuery);
    if (state.queueStatus) params.set('status', state.queueStatus);
    const tbody = document.getElementById('ws-order-rows');
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-3">در حال خواندن…</td></tr>';
    try {
      const data = await api(`${CONFIG.orders}?${params}`);
      document.getElementById('ws-order-count').textContent = `${fmt(data.count)} سفارش`;
      document.getElementById('ws-queue-page').textContent = `صفحهٔ ${fmt(data.page)} از ${fmt(data.pages)}`;
      tbody.innerHTML = data.rows.length ? '' : '<tr><td colspan="7" class="text-center text-muted py-4">سفارشی برای برش پیدا نشد؛ ممکن است در برنامه‌های فعال رزرو باشند.</td></tr>';
      data.rows.forEach(row => tbody.appendChild(queueRow(row)));
      state.queuePages = data.pages;
    } catch (error) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center text-danger py-3">${esc(error.message)}</td></tr>`;
    }
  }

  function queueRow(row) {
    const tr = document.createElement('tr');
    tr.className = 'ws-order-row';
    tr.dataset.orderId = row.id;
    const inPlan = row.planning_state === 'in_plan';
    tr.innerHTML = `
      <td><input class="form-check-input ws-order-select" type="checkbox" value="${row.id}" aria-label="انتخاب سفارش ${row.id}"></td>
      <td><b class="num">#${row.id}</b>${row.number ? ` · ${esc(row.number)}` : ''}<div class="small text-muted">${esc(row.customer_name)}</div></td>
      <td class="d-none d-lg-table-cell small">${esc(row.rep_name) || '—'}</td>
      <td class="d-none d-md-table-cell text-nowrap small num">${esc(row.date_jalali)}</td>
      <td class="d-none d-md-table-cell num">${fmt(row.item_count)}</td>
      <td class="fw-bold num">${fmt(row.remaining_pieces)}</td>
      <td>${inPlan ? '<span class="badge bg-secondary">در برنامهٔ فعال</span>' : '<span class="badge bg-light text-dark">آزاد</span>'}</td>`;
    tr.querySelector('.ws-order-select').checked = state.selection.has(String(row.id));
    tr.querySelector('.ws-order-select').addEventListener('change', event => {
      const id = String(row.id);
      if (event.target.checked) state.selection.add(id); else state.selection.delete(id);
      tr.classList.toggle('ws-order-selected', event.target.checked);
      syncSelection();
    });
    tr.addEventListener('click', event => {
      if (event.target.closest('input, a, button')) return;
      focusOrder(row.id);
    });
    return tr;
  }

  function syncSelection() {
    if (!form) return;
    form.querySelectorAll('input[name="order_ids"]').forEach(input => input.remove());
    state.selection.forEach(id => {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = 'order_ids'; input.value = id;
      form.appendChild(input);
    });
    document.dispatchEvent(new CustomEvent('ws:selection-changed', { detail: { ids: [...state.selection] } }));
  }

  /* ---------- focused order: items + materials + parts ---------- */

  async function focusOrder(orderId) {
    state.focusId = orderId;
    document.querySelectorAll('.ws-order-row').forEach(row =>
      row.classList.toggle('ws-order-focused', row.dataset.orderId === String(orderId)));
    const card = document.getElementById('ws-parts-card');
    card.hidden = false;
    document.getElementById('ws-focus-title').textContent = `سفارش #${orderId} — در حال خواندن…`;
    try {
      const data = await api(pattern(CONFIG.order_pattern, orderId));
      state.orderInfo = data.order;
      state.items = data.items;
      state.materials = data.materials;
      state.item = 'all'; state.material = 'all'; state.partsPage = 1;
      renderChips();
      document.getElementById('ws-focus-title').textContent =
        `سفارش #${data.order.id}${data.order.number ? ` · ${data.order.number}` : ''} — ${data.order.customer_name}`;
      await loadParts();
    } catch (error) {
      document.getElementById('ws-focus-title').textContent = `سفارش #${orderId}`;
      announce(error.message);
    }
  }

  function chip(label, active, onClick, title = '') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `ws-chip${active ? ' active' : ''}`;
    button.title = title;
    button.innerHTML = label;
    button.addEventListener('click', onClick);
    return button;
  }

  function renderChips() {
    const itemHolder = document.getElementById('ws-item-chips');
    const materialHolder = document.getElementById('ws-material-chips');
    itemHolder.replaceChildren();
    materialHolder.replaceChildren();
    itemHolder.appendChild(chip('همهٔ آیتم‌ها', state.item === 'all', () => {
      state.item = 'all'; state.partsPage = 1; renderChips(); loadParts();
    }));
    state.items.forEach(item => {
      itemHolder.appendChild(chip(`#${item.id} ${esc(item.product_name)} (${fmt(item.piece_count)})`,
        state.item === String(item.id), () => {
          state.item = state.item === String(item.id) ? 'all' : String(item.id);
          state.partsPage = 1; renderChips(); loadParts();
        }, item.material_names.join('، ')));
    });
    materialHolder.appendChild(chip('همهٔ متریال‌ها', state.material === 'all', () => {
      state.material = 'all'; state.partsPage = 1; renderChips(); loadParts();
    }));
    state.materials.forEach(material => {
      materialHolder.appendChild(chip(esc(material.name), state.material === String(material.id), () => {
        state.material = state.material === String(material.id) ? 'all' : String(material.id);
        state.partsPage = 1; renderChips(); loadParts();
      }));
    });
  }

  async function loadParts() {
    if (!state.focusId) return;
    const params = new URLSearchParams({
      item: state.item, material: state.material, page: state.partsPage,
    });
    if (state.search) params.set('q', state.search);
    if (state.sort) params.set('sort', state.sort);
    const tbody = document.getElementById('ws-part-rows');
    tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted py-3">در حال خواندن قطعات…</td></tr>';
    try {
      const data = await api(`${pattern(CONFIG.parts_pattern, state.focusId)}?${params}`);
      state.rows = Object.fromEntries(data.rows.map(row => [row.task_id, row]));
      state.partsPages = data.pages;
      tbody.innerHTML = data.rows.length ? '' :
        '<tr><td colspan="10" class="text-center text-muted py-4">قطعه‌ای با این فیلترها پیدا نشد.</td></tr>';
      data.rows.forEach(row => tbody.appendChild(partRow(row)));
      document.getElementById('ws-parts-page').textContent = `صفحهٔ ${fmt(data.page)} از ${fmt(data.pages)}`;
      const summary = document.getElementById('ws-summary');
      summary.innerHTML = `جمع ${fmt(data.count)} قطعه — نیاز هر متریال: ` + data.summary.map(entry =>
        `<span class="badge bg-light text-dark">${esc(entry.name)}: <b class="num">${fmt(entry.piece_need)}</b></span>`
      ).join(' ');
      document.querySelectorAll('.ws-sortable').forEach(th =>
        th.classList.toggle('ws-sorted', th.dataset.sort === state.sort.replace('-', '')));
    } catch (error) {
      tbody.innerHTML = `<tr><td colspan="10" class="text-center text-danger py-3">${esc(error.message)}</td></tr>`;
    }
  }

  /* ---------- parts table rows ---------- */

  const DIM_FIELDS = { length: 'طول', width: 'عرض', quantity_total: 'تعداد' };

  function partRow(row) {
    const tr = document.createElement('tr');
    tr.dataset.taskId = row.task_id;
    tr.dataset.version = row.version;
    tr.className = row.status_code === 'locked_in_plan' ? 'ws-row-locked'
      : row.excluded ? 'ws-row-excluded'
      : row.overridden_fields.length ? 'ws-row-changed' : '';
    const lock = row.reserved_plan
      ? `<div class="ws-lock-note">قفل: ${esc(row.status_label)}${row.reserved_plan.kind === 'cut'
          ? ` — <a href="${pattern(CONFIG.cut_plan_pattern, row.reserved_plan.id)}">برنامه #${row.reserved_plan.id}</a>`
          : ` — <a href="${CONFIG.cnc_plans}">برنامهٔ CNC #${row.reserved_plan.id}</a>`}</div>`
      : '';
    const editable = row.editable && !row.excluded;
    const cncHint = row.is_cnc_first ? ' title="قطعهٔ CNC: تغییر عرض یا متریالِ با ضخامت متفاوت به صف اصلاح دستی می‌رود."' : '';
    const edgeInput = (field) => `
      <input class="form-control form-control-sm cell-input cell-edge" data-field="${field}"
             value="${esc(row.edges[field] || '')}" maxlength="100" ${editable ? '' : 'disabled'}
             aria-label="${EDGE_LABELS[field]} ${esc(row.name)}" placeholder="—">`;
    tr.innerHTML = `
      <td><b>${esc(row.name)}</b><div class="small text-muted">${esc(row.pname)}</div>${lock}</td>
      <td class="d-none d-lg-table-cell small num">${esc(row.barcode) || '—'}</td>
      <td><input class="form-control form-control-sm cell-input cell-dim" data-field="length" type="number" min="0" step="0.1" value="${row.length}" ${editable ? '' : 'disabled'} aria-label="طول ${esc(row.name)}"></td>
      <td><input class="form-control form-control-sm cell-input cell-dim" data-field="width" type="number" min="0" step="0.1" value="${row.width}" ${editable ? '' : 'disabled'}${row.is_cnc_first ? ' title="قطعهٔ CNC: تغییر عرض به صف اصلاح دستی می‌رود."' : ''} aria-label="عرض ${esc(row.name)}"></td>
      <td style="min-width:5.5rem"><input class="form-control form-control-sm cell-input cell-dim" data-field="quantity_total" type="number" min="0" step="1" value="${row.quantity_total}" ${editable ? '' : 'disabled'} aria-label="تعداد ${esc(row.name)}"><div class="small text-muted num">باقی: ${fmt(row.quantity_remaining)} · انجام: ${fmt(row.quantity_completed)}</div></td>
      <td><select class="form-select form-select-sm cell-input cell-material" ${editable ? '' : 'disabled'} aria-label="متریال ${esc(row.name)}">
        ${MATERIALS.map(m => `<option value="${m.id}" ${String(m.id) === String(row.material_id) ? 'selected' : ''}>${esc(m.name)} (${m.thickness})</option>`).join('')}
      </select></td>
      <td><input class="form-check-input cell-turn" type="checkbox" ${row.turn ? 'checked' : ''} ${editable ? '' : 'disabled'} aria-label="چرخش ${esc(row.name)}"></td>
      <td class="d-none d-xl-table-cell ws-edge-cell">${edgeInput('f4')}${edgeInput('f5')}${edgeInput('f18')}${edgeInput('f26')}</td>
      <td><span class="badge ws-status ws-status-${row.status_code}" title="${esc(row.excluded ? `علت: ${row.excluded_reason}` : row.status_label)}">${esc(row.status_label)}</span>${row.overridden_fields.length ? '<div class="small text-warning">● تغییرکرده</div>' : ''}</td>
      <td class="text-nowrap ws-actions">${actionsHtml(row, editable)}</td>`;
    return tr;
  }

  function actionsHtml(row, editable) {
    const buttons = [];
    if (row.excluded) {
      buttons.push(`<button type="button" class="btn btn-sm btn-outline-success ws-include" data-task="${row.task_id}">بازگرداندن</button>`);
    } else if (editable) {
      buttons.push(`<button type="button" class="btn btn-sm btn-outline-danger ws-exclude" data-task="${row.task_id}" title="خارج‌کردن از برنامهٔ برش/CNC (بدون حذف از سفارش)">خارج از برنامه</button>`);
    }
    if (row.version && row.overridden_fields.length && row.editable) {
      buttons.push(`<button type="button" class="btn btn-sm btn-outline-secondary ws-reset" data-task="${row.task_id}" title="بازگشت به مشخصات پایه">بازنشانی</button>`);
    }
    return buttons.join(' ');
  }

  function replaceRow(row) {
    state.rows[row.task_id] = row;
    const tbody = document.getElementById('ws-part-rows');
    const old = tbody.querySelector(`tr[data-task-id="${row.task_id}"]`);
    if (old) old.replaceWith(partRow(row));
  }

  /* ---------- inline editing ---------- */

  function cellState(input, text, kind) {
    if (!input) { announce(String(text || '')); return; }
    const holder = input.closest('td') || input.closest('tr');
    let note = holder.querySelector('.ws-cell-state');
    if (!note) {
      note = document.createElement('div');
      note.className = 'ws-cell-state';
      holder.appendChild(note);
    }
    note.textContent = text || '';
    note.className = `ws-cell-state ${kind || ''}`;
  }

  async function patchField(tr, field, value) {
    const taskId = tr.dataset.taskId;
    const version = Number(tr.dataset.version || 0);
    const domField = field === 'quantity' ? 'quantity_total' : field;
    const input = tr.querySelector(`[data-field="${domField}"]`);
    const previous = input ? input.defaultValue : '';
    input?.setAttribute('disabled', 'disabled');
    cellState(input, 'در حال ذخیره…', 'text-muted');
    const freshInput = () => table.querySelector(`tr[data-task-id="${taskId}"] [data-field="${domField}"]`);
    try {
      const data = await api(pattern(CONFIG.spec_pattern, taskId), {
        method: 'PATCH',
        body: JSON.stringify({ version, fields: { [field]: value }, reason: '' }),
      });
      replaceRow(data.row);
      cellState(freshInput(), data.message, 'text-success');
      announce(`${data.message} (${field})`);
      document.dispatchEvent(new CustomEvent('ws:specs-changed'));
    } catch (error) {
      if (error.status === 409 && error.payload?.row) {
        replaceRow(error.payload.row);
        announce('این ردیف جای دیگر تغییر کرده بود؛ ردیف تازه شد.');
        cellState(freshInput(), 'ناسازگاری نسخه؛ ردیف دوباره بارگذاری شد', 'text-danger');
        return;
      }
      if (input) { input.value = previous; input.removeAttribute('disabled'); }
      cellState(input, error.message, 'text-danger');
      announce(error.message);
    }
  }

  async function postAction(action, tr, extra = {}) {
    const taskId = tr.dataset.taskId;
    try {
      const url = pattern(CONFIG[`${action}_pattern`], taskId);
      const data = await api(url, { method: 'POST', body: JSON.stringify({ version: Number(tr.dataset.version || 0), ...extra }) });
      replaceRow(data.row);
      announce(data.message);
      document.dispatchEvent(new CustomEvent('ws:specs-changed'));
    } catch (error) {
      if (error.status === 409 && error.payload?.row) { replaceRow(error.payload.row); }
      announce(error.message);
    }
  }

  const table = document.getElementById('ws-part-rows');
  table.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.matches('.cell-input')) {
      event.preventDefault();
      event.target.dispatchEvent(new Event('focusout'));
      event.target.blur();
    }
  });
  table.addEventListener('focusout', event => {
    const input = event.target;
    if (!input.matches('.cell-input')) return;
    const tr = input.closest('tr');
    const row = state.rows[tr.dataset.taskId];
    if (!row || row.status_code === 'locked_in_plan') return;
    if (input.classList.contains('cell-dim')) {
      const field = input.dataset.field === 'quantity_total' ? 'quantity' : input.dataset.field;
      const current = field === 'quantity'
        ? String(row.quantity_total)
        : String(row[field]);
      if (input.value === current) return;
      patchField(tr, field, input.value);
    } else if (input.classList.contains('cell-material')) {
      if (String(input.value) === String(row.material_id)) return;
      patchField(tr, 'material', input.value);
    } else if (input.classList.contains('cell-edge')) {
      const field = input.dataset.field;
      if ((input.value || '') === (row.edges[field] || '')) return;
      patchField(tr, field, input.value);
    }
  });
  table.addEventListener('change', event => {
    if (!event.target.matches('.cell-turn')) return;
    patchField(event.target.closest('tr'), 'turn', event.target.checked);
  });
  table.addEventListener('click', event => {
    const exclude = event.target.closest('.ws-exclude');
    if (exclude) { openExcludeModal(Number(exclude.dataset.task)); return; }
    const include = event.target.closest('.ws-include');
    if (include) { postAction('include', include.closest('tr')); return; }
    const reset = event.target.closest('.ws-reset');
    if (reset) { postAction('reset', reset.closest('tr')); }
  });

  /* ---------- exclude modal ---------- */

  function closeModal() {
    const holder = document.getElementById('ws-modal-holder');
    holder.replaceChildren();
  }

  function openExcludeModal(taskId) {
    const row = state.rows[taskId];
    const holder = document.getElementById('ws-modal-holder');
    holder.innerHTML = `
      <div class="ws-modal-backdrop" role="presentation">
        <div class="ws-modal card" role="dialog" aria-modal="true" aria-labelledby="ws-exclude-title">
          <div class="card-header py-2"><b id="ws-exclude-title">خارج‌کردن «${esc(row.name)}» از برنامه</b></div>
          <div class="card-body">
            <p class="small text-muted mb-2">قطعه از برنامهٔ برش/CNC کنار گذاشته می‌شود ولی از سفارش و اطلاعات پایه محصول حذف نمی‌شود و بعداً قابل بازگشت است.</p>
            <label class="form-label" for="ws-exclude-reason">علت (الزامی)</label>
            <input id="ws-exclude-reason" class="form-control form-control-sm" maxlength="255" placeholder="مثلاً «موجود در انبار» یا «قبلاً تولید شده»">
            <div id="ws-exclude-error" class="text-danger small mt-1"></div>
          </div>
          <div class="card-footer d-flex justify-content-end gap-2 py-2">
            <button type="button" class="btn btn-sm btn-outline-secondary" id="ws-exclude-cancel">انصراف</button>
            <button type="button" class="btn btn-sm btn-danger" id="ws-exclude-confirm">خارج‌کردن از برنامه</button>
          </div>
        </div>
      </div>`;
    const reason = holder.querySelector('#ws-exclude-reason');
    reason.focus();
    holder.querySelector('#ws-exclude-cancel').addEventListener('click', closeModal);
    holder.querySelector('.ws-modal-backdrop').addEventListener('click', event => {
      if (event.target.classList.contains('ws-modal-backdrop')) closeModal();
    });
    holder.querySelector('#ws-exclude-confirm').addEventListener('click', async () => {
      const value = reason.value.trim();
      if (!value) {
        holder.querySelector('#ws-exclude-error').textContent = 'ثبت علت الزامی است.';
        return;
      }
      const tr = table.querySelector(`tr[data-task-id="${taskId}"]`);
      await postAction('exclude', tr, { reason: value });
      closeModal();
    });
  }

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeModal();
  });

  /* ---------- queue toolbar + pagination + chips/search/sort ---------- */

  let searchTimer = null;
  document.getElementById('ws-order-search').addEventListener('input', event => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.queueQuery = event.target.value.trim();
      state.queuePage = 1;
      loadQueue();
    }, 400);
  });
  document.getElementById('ws-order-status').addEventListener('change', event => {
    state.queueStatus = event.target.value;
    state.queuePage = 1;
    loadQueue();
  });
  document.getElementById('ws-queue-prev').addEventListener('click', () => {
    if (state.queuePage > 1) { state.queuePage -= 1; loadQueue(); }
  });
  document.getElementById('ws-queue-next').addEventListener('click', () => {
    if (state.queuePage < state.queuePages) { state.queuePage += 1; loadQueue(); }
  });
  let partsSearchTimer = null;
  document.getElementById('ws-parts-search').addEventListener('input', event => {
    clearTimeout(partsSearchTimer);
    partsSearchTimer = setTimeout(() => {
      state.search = event.target.value.trim();
      state.partsPage = 1;
      loadParts();
    }, 350);
  });
  document.getElementById('ws-parts-prev').addEventListener('click', () => {
    if (state.partsPage > 1) { state.partsPage -= 1; loadParts(); }
  });
  document.getElementById('ws-parts-next').addEventListener('click', () => {
    if (state.partsPage < state.partsPages) { state.partsPage += 1; loadParts(); }
  });
  document.querySelector('.ws-edit-grid thead').addEventListener('click', event => {
    const th = event.target.closest('.ws-sortable');
    if (!th) return;
    const field = th.dataset.sort;
    state.sort = state.sort === field ? `-${field}` : field;
    state.partsPage = 1;
    loadParts();
  });

  /* ---------- init ---------- */

  const statusSelect = document.getElementById('ws-order-status');
  (async () => {
    try {
      const data = await api(CONFIG.orders + '?page_size=1');
      data.statuses.forEach(([value, label]) => {
        const option = document.createElement('option');
        option.value = value; option.textContent = label;
        statusSelect.appendChild(option);
      });
    } catch (error) { /* status list is cosmetic */ }
    loadQueue();
  })();
})();
