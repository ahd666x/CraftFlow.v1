/* فضای کاری برش v2 — صف سفارش‌ها به‌صورت لیست اصلی صفحه و جدول قطعات
 * قابل‌ویرایش درون «ردیف بازشونده» همان سفارش (بدون کارت جدا).
 * داده‌ها از API فضای کاری (cutting.workspace_views) می‌آید؛ منطق PATCH/409/قفل
 * و پیام‌ها دست‌نخورده مانده است. جست‌وجوی سفارش در سربرگ صفحه است (ws-order-search).
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
    rows: {}, partsPage: 1, partsPages: 1,
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
    <div class="queue-head" aria-hidden="true"><span></span><span>سفارش / مشتری</span><span class="text-start">قطعه</span><span class="text-start">وضعیت</span><span></span></div>
    <div class="queue" id="ws-order-rows"></div>
    <div id="ws-modal-holder"></div>
    <p id="ws-live" class="visually-hidden" aria-live="polite"></p>
  `;
  const queueEl = document.getElementById('ws-order-rows');

  /* ---------- order queue ---------- */

  async function loadQueue() {
    const params = new URLSearchParams({ page: state.queuePage });
    if (state.queueQuery) params.set('q', state.queueQuery);
    if (state.queueStatus) params.set('status', state.queueStatus);
    state.focusId = null;
    queueEl.innerHTML = '<div class="ws-empty"><span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span></div>';
    try {
      const data = await api(`${CONFIG.orders}?${params}`);
      queueEl.innerHTML = data.rows.length ? '' :
        '<div class="ws-empty"><i class="bi bi-inbox" aria-hidden="true"></i></div>';
      data.rows.forEach(row => queueEl.appendChild(queueRow(row)));
      state.queuePages = data.pages;
    } catch (error) {
      queueEl.innerHTML = `<div class="ws-empty text-danger">${esc(error.message)}</div>`;
    }
  }

  function queueRow(row) {
    const el = document.createElement('div');
    el.className = 'queue-row';
    el.dataset.orderId = row.id;
    const inPlan = row.planning_state === 'in_plan';
    el.innerHTML = `
      <span class="q-check"><input class="form-check-input ws-order-select" type="checkbox" value="${row.id}" aria-label="انتخاب سفارش ${row.id}"></span>
      <span class="q-order"><b class="num">#${row.id}</b>${row.number ? ` <span class="num help">· ${esc(row.number)}</span>` : ''}<span class="q-cust" title="نماینده: ${esc(row.rep_name) || '—'}">${esc(row.customer_name)}</span></span>
      <span class="q-count num">${fmt(row.remaining_pieces)}<small class="num">${fmt(row.item_count)} آیتم</small></span>
      <span class="q-status"><span class="badge-status ${inPlan ? 'is-warn' : 'is-mute'}">${inPlan ? 'در برنامه' : 'آزاد'}</span></span>
      <span class="q-expand"><button type="button" class="row-expand-toggle" aria-expanded="false" aria-controls="wsd-${row.id}" title="آیتم‌ها، متریال‌ها و جدول قطعات" aria-label="جزئیات سفارش ${row.id}"><i class="bi bi-chevron-down" aria-hidden="true"></i></button></span>`;
    el.querySelector('.ws-order-select').checked = state.selection.has(String(row.id));
    if (state.selection.has(String(row.id))) el.classList.add('is-selected');
    el.querySelector('.ws-order-select').addEventListener('change', event => {
      const id = String(row.id);
      if (event.target.checked) state.selection.add(id); else state.selection.delete(id);
      el.classList.toggle('is-selected', event.target.checked);
      syncSelection();
    });
    el.querySelector('.q-expand .row-expand-toggle').addEventListener('click', () => focusOrder(row.id));
    el.addEventListener('click', event => {
      if (event.target.closest('input, a, button, select')) return;
      focusOrder(row.id);
    });
    return el;
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

  /* ---------- expanded order: meta + chips + parts table ---------- */

  async function focusOrder(orderId) {
    const rowEl = queueEl.querySelector(`.queue-row[data-order-id="${orderId}"]`);
    if (!rowEl) return;
    const toggle = rowEl.querySelector('.row-expand-toggle');
    if (String(state.focusId) === String(orderId)) {
      closeDetails(rowEl);
      return;
    }
    queueEl.querySelectorAll('.queue-details').forEach(node => node.remove());
    queueEl.querySelectorAll('.queue-row').forEach(node => {
      node.classList.remove('is-focused');
      node.querySelector('.row-expand-toggle')?.setAttribute('aria-expanded', 'false');
    });
    state.focusId = orderId;
    state.item = 'all'; state.material = 'all'; state.search = ''; state.sort = '';
    state.partsPage = 1; state.rows = {};
    rowEl.classList.add('is-focused');
    toggle?.setAttribute('aria-expanded', 'true');

    const details = document.createElement('div');
    details.className = 'queue-details';
    details.id = `wsd-${orderId}`;
    details.innerHTML = '<div class="help">در حال خواندن…</div>';
    rowEl.insertAdjacentElement('afterend', details);
    try {
      const data = await api(pattern(CONFIG.order_pattern, orderId));
      state.orderInfo = data.order;
      state.items = data.items;
      state.materials = data.materials;
      details.innerHTML = detailsTemplate(data.order);
      renderChips();
      await loadParts();
    } catch (error) {
      details.innerHTML = `<div class="ws-empty text-danger">${esc(error.message)}</div>`;
      announce(error.message);
    }
  }

  function closeDetails(rowEl) {
    rowEl?.querySelector('.row-expand-toggle')?.setAttribute('aria-expanded', 'false');
    rowEl?.classList.remove('is-focused');
    document.getElementById(`wsd-${rowEl?.dataset.orderId}`)?.remove();
    state.focusId = null;
  }

  function detailsTemplate(order) {
    return `
      <div class="qd-meta">
        <span>تاریخ <b class="num">${esc(order.date_jalali || '—')}</b></span>
        <span>نماینده <b>${esc(order.rep_name || '—')}</b></span>
        <span>مشتری <b>${esc(order.customer_name)}</b></span>
      </div>
      <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
        <span class="help">آیتم‌ها:</span><div class="ws-chips" data-ws-item-chips></div>
        <span class="help">متریال‌ها:</span><div class="ws-chips" data-ws-material-chips></div>
        <span class="input-search ms-auto"><i class="bi bi-search" aria-hidden="true"></i><input class="form-control form-control-sm ws-parts-search" type="search" placeholder="قطعه یا بارکد" aria-label="جست‌وجوی قطعه" autocomplete="off"></span>
      </div>
      <div class="table-responsive"><table class="table table-min ws-part-table mb-0">
        <thead><tr>
          <th>قطعه</th>
          <th class="ws-sortable" data-sort="length">طول</th><th class="ws-sortable" data-sort="width">عرض</th>
          <th class="ws-sortable" data-sort="quantity">تعداد</th>
          <th class="ws-sortable" data-sort="material">متریال</th><th title="چرخش ۹۰ درجه">چرخش</th>
          <th class="ws-sortable" data-sort="status">وضعیت</th><th><span class="visually-hidden">جزئیات بیشتر</span></th>
        </tr></thead>
        <tbody class="ws-part-rows"></tbody>
      </table></div>
      <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mt-2">
        <span class="ws-summary help"></span>
        <span class="pager m-0 p-0"><button type="button" class="btn btn-sm btn-outline-secondary ws-parts-prev">قبلی</button><span class="ws-parts-page help num"></span><button type="button" class="btn btn-sm btn-outline-secondary ws-parts-next">بعدی</button></span>
      </div>`;
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
    const itemHolder = root.querySelector('[data-ws-item-chips]');
    const materialHolder = root.querySelector('[data-ws-material-chips]');
    if (!itemHolder || !materialHolder) return;
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
    const tbody = root.querySelector('.ws-part-rows');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3">در حال خواندن قطعات…</td></tr>';
    try {
      const data = await api(`${pattern(CONFIG.parts_pattern, state.focusId)}?${params}`);
      state.rows = Object.fromEntries(data.rows.map(row => [row.task_id, row]));
      state.partsPages = data.pages;
      tbody.innerHTML = data.rows.length ? '' :
        '<tr><td colspan="8" class="text-center text-muted py-4">قطعه‌ای با این فیلترها پیدا نشد.</td></tr>';
      data.rows.forEach(row => tbody.appendChild(partRow(row)));
      const pageLabel = root.querySelector('.ws-parts-page');
      if (pageLabel) pageLabel.textContent = `صفحهٔ ${fmt(data.page)} از ${fmt(data.pages)}`;
      const summary = root.querySelector('.ws-summary');
      if (summary) summary.innerHTML = `جمع ${fmt(data.count)} قطعه — نیاز هر متریال: ` + data.summary.map(entry =>
        `<span class="badge-status">${esc(entry.name)}: <b class="num">${fmt(entry.piece_need)}</b></span>`
      ).join(' ');
      root.querySelectorAll('.ws-sortable').forEach(th =>
        th.classList.toggle('ws-sorted', th.dataset.sort === state.sort.replace('-', '')));
    } catch (error) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-danger py-3">${esc(error.message)}</td></tr>`;
    }
  }

  /* ---------- parts table rows ---------- */

  function partRow(row) {
    // دو ردیف: اصلی (حداقل: قطعه/طول/عرض/تعداد/متریال + چرخش و وضعیت) و ردیف جزئیات (بارکد، نوار لبه، وضعیت کامل، اقدامات)
    const frag = document.createDocumentFragment();
    const tr = document.createElement('tr');
    tr.dataset.taskId = row.task_id;
    tr.dataset.version = row.version;
    tr.className = row.status_code === 'locked_in_plan' ? 'ws-row-locked'
      : row.excluded ? 'ws-row-excluded'
      : row.overridden_fields.length ? 'ws-row-changed' : '';
    const editable = row.editable && !row.excluded;
    const cncWidthHint = row.is_cnc_first ? ' title="قطعهٔ CNC: تغییر عرض به صف اصلاح دستی می‌رود."' : '';
    const statusTitle = row.excluded ? `علت: ${row.excluded_reason}` : row.status_label;
    const detailsId = `ws-part-details-${row.task_id}`;
    tr.innerHTML = `
      <td class="td-lead"><b>${esc(row.name)}</b><div class="small text-muted">${esc(row.pname)}</div></td>
      <td data-label="طول"><input class="form-control form-control-sm cell-input cell-dim" data-field="length" type="number" min="0" step="0.1" value="${row.length}" ${editable ? '' : 'disabled'} aria-label="طول ${esc(row.name)}"></td>
      <td data-label="عرض"><input class="form-control form-control-sm cell-input cell-dim" data-field="width" type="number" min="0" step="0.1" value="${row.width}" ${editable ? '' : 'disabled'}${cncWidthHint} aria-label="عرض ${esc(row.name)}"></td>
      <td data-label="تعداد"><input class="form-control form-control-sm cell-input cell-dim" data-field="quantity_total" type="number" min="0" step="1" value="${row.quantity_total}" ${editable ? '' : 'disabled'} aria-label="تعداد ${esc(row.name)}"></td>
      <td data-label="متریال"><select class="form-select form-select-sm cell-input cell-material" ${editable ? '' : 'disabled'} aria-label="متریال ${esc(row.name)}">
        ${MATERIALS.map(m => `<option value="${m.id}" ${String(m.id) === String(row.material_id) ? 'selected' : ''}>${esc(m.name)} (${m.thickness})</option>`).join('')}
      </select></td>
      <td data-label="چرخش"><input class="form-check-input cell-turn" type="checkbox" ${row.turn ? 'checked' : ''} ${editable ? '' : 'disabled'} aria-label="چرخش ${esc(row.name)}" title="چرخش ۹۰ درجه"></td>
      <td data-label="وضعیت"><span class="badge-status ${row.excluded ? 'is-bad' : row.status_code === 'locked_in_plan' ? 'is-warn' : 'is-mute'}" title="${esc(statusTitle)}">${esc(row.status_label)}</span>${row.overridden_fields.length ? ' <span class="text-warning" title="مشخصات از پایه تغییر کرده">●</span>' : ''}</td>
      <td class="text-nowrap text-end"><button type="button" class="row-expand-toggle" data-row-expand aria-expanded="false" aria-controls="${detailsId}" title="بارکد، نوار لبه، وضعیت و اقدامات" aria-label="جزئیات بیشتر ${esc(row.name)}"><i class="bi bi-chevron-down" aria-hidden="true"></i></button></td>`;
    frag.appendChild(tr);

    const lock = row.reserved_plan
      ? `<div class="ws-lock-note">قفل: ${esc(row.status_label)}${row.reserved_plan.kind === 'cut'
          ? ` — <a href="${pattern(CONFIG.cut_plan_pattern, row.reserved_plan.id)}">برنامه #${row.reserved_plan.id}</a>`
          : ` — <a href="${CONFIG.cnc_plans}">برنامهٔ CNC #${row.reserved_plan.id}</a>`}</div>`
      : '';
    const edgeInput = (field) => `
      <input class="form-control form-control-sm cell-input cell-edge" data-field="${field}"
             value="${esc(row.edges[field] || '')}" maxlength="100" ${editable ? '' : 'disabled'}
             aria-label="${EDGE_LABELS[field]} ${esc(row.name)}" placeholder="—">`;
    const det = document.createElement('tr');
    det.id = detailsId;
    det.className = 'row-expand-details';
    det.hidden = true;
    det.dataset.detailsFor = row.task_id;
    det.dataset.taskId = row.task_id;
    det.dataset.version = row.version;
    det.innerHTML = `
      <td colspan="8">
        <div class="row g-2 align-items-start">
          <div class="col-6 col-md-3"><span class="d-block small text-muted">بارکد</span><span class="num">${esc(row.barcode) || '—'}</span></div>
          <div class="col-6 col-md-4"><span class="d-block small text-muted">تعداد</span><span class="num small">باقی ${fmt(row.quantity_remaining)} · انجام ${fmt(row.quantity_completed)}</span></div>
          <div class="col-12">${lock}</div>
          <div class="col-12"><span class="d-block small text-muted">نوار لبه — طول۱/طول۲/عرض۱/عرض۲</span><div class="ws-edge-cell d-flex flex-wrap gap-1">${edgeInput('f4')}${edgeInput('f5')}${edgeInput('f18')}${edgeInput('f26')}</div></div>
          <div class="col-12 d-flex flex-wrap align-items-center gap-2">
            <span class="badge-status ${row.excluded ? 'is-bad' : row.status_code === 'locked_in_plan' ? 'is-warn' : 'is-mute'}" title="${esc(statusTitle)}">${esc(row.status_label)}</span>
            ${row.excluded ? `<span class="small text-danger">علت: ${esc(row.excluded_reason)}</span>` : ''}
            <span class="ms-auto d-flex flex-wrap gap-1">${actionsHtml(row, editable)}</span>
          </div>
        </div>
      </td>`;
    frag.appendChild(det);
    return frag;
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
    const tbody = root.querySelector('.ws-part-rows');
    if (!tbody) return;
    const old = tbody.querySelector(`tr[data-task-id="${row.task_id}"]:not(.row-expand-details)`);
    if (old) {
      tbody.querySelector(`tr[data-details-for="${row.task_id}"]`)?.remove();
      old.replaceWith(partRow(row));
    }
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
    const freshInput = () => root.querySelector(`tr[data-task-id="${taskId}"] [data-field="${domField}"]`);
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

  root.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.matches('.cell-input')) {
      event.preventDefault();
      event.target.dispatchEvent(new Event('focusout'));
      event.target.blur();
    }
  });
  root.addEventListener('focusout', event => {
    const input = event.target;
    if (!input.matches('.cell-input')) return;
    const tr = input.closest('tr');
    const row = state.rows[tr?.dataset.taskId];
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
  root.addEventListener('change', event => {
    if (!event.target.matches('.cell-turn')) return;
    patchField(event.target.closest('tr'), 'turn', event.target.checked);
  });
  root.addEventListener('click', event => {
    const exclude = event.target.closest('.ws-exclude');
    if (exclude) { openExcludeModal(Number(exclude.dataset.task)); return; }
    const include = event.target.closest('.ws-include');
    if (include) { postAction('include', include.closest('tr')); return; }
    const reset = event.target.closest('.ws-reset');
    if (reset) { postAction('reset', reset.closest('tr')); return; }
    const sortable = event.target.closest('.ws-sortable');
    if (sortable) {
      const field = sortable.dataset.sort;
      state.sort = state.sort === field ? `-${field}` : field;
      state.partsPage = 1;
      loadParts();
      return;
    }
    if (event.target.closest('.ws-parts-prev')) {
      if (state.partsPage > 1) { state.partsPage -= 1; loadParts(); }
      return;
    }
    if (event.target.closest('.ws-parts-next')) {
      if (state.partsPage < state.partsPages) { state.partsPage += 1; loadParts(); }
    }
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
      const tr = root.querySelector(`tr[data-task-id="${taskId}"]`);
      await postAction('exclude', tr, { reason: value });
      closeModal();
    });
  }

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeModal();
  });

  /* ---------- queue toolbar + parts search (delegated) ---------- */

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
  let partsSearchTimer = null;
  root.addEventListener('input', event => {
    if (!event.target.matches('.ws-parts-search')) return;
    clearTimeout(partsSearchTimer);
    partsSearchTimer = setTimeout(() => {
      state.search = event.target.value.trim();
      state.partsPage = 1;
      loadParts();
    }, 350);
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
