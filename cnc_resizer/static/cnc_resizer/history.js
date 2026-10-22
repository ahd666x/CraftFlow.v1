/* ============================================================
   history.js — Undo/Redo کامل و «الگو» (پرامپت ۲، فاز «ج»)
   * هر commit یک snapshot از وضعیت ویرایش‌ها (مطلق نسبت به فایل اصلی)
   * Ctrl+Z / Ctrl+Shift+Z (و Ctrl+Y) + دکمه‌های نوار ابزار
   * تاریخچه به‌صورت لیست قابل مرور و پرش
   * ذخیرهٔ وضعیت فعلی به‌عنوان الگو از EditTemplate موجود + بارگذاری
     (الگو هرگز خودش اجرا نمی‌شود؛ هر بار از زنجیرهٔ fail-closed می‌گذرد)
   ============================================================ */
(() => {
  'use strict';
  const CNC = window.CNC;
  const state = CNC.state;

  const stack = [];          // [{label, ops}] — ops = {partOp: [[id, op]], featureOps: [...]}
  let cursor = -1;           // وضعیت جاری = stack[cursor]

  function snapshot(label) {
    return {
      label,
      ops: {
        partOp: [...state.partOp.entries()].map(([id, op]) => [id, { ...op }]),
        featureOps: state.featureOps.map((op) => ({ ...op })),
      },
    };
  }

  function restore(snap) {
    state.partOp = new Map(snap.ops.partOp);
    state.featureOps = snap.ops.featureOps.slice();
    CNC.emit('ops-restored', { label: snap.label });
    CNC.requestPreview();
  }

  function push(label) {
    // هر commit جدید، شاخهٔ redo را حذف می‌کند
    stack.splice(cursor + 1);
    stack.push(snapshot(label));
    if (stack.length > 100) stack.shift();   // سقف معقول حافظه
    cursor = stack.length - 1;
    renderPanel();
    syncButtons();
  }

  function undo() {
    if (cursor <= 0) return;
    cursor -= 1;
    restore(stack[cursor]);
    renderPanel();
    syncButtons();
  }
  function redo() {
    if (cursor >= stack.length - 1) return;
    cursor += 1;
    restore(stack[cursor]);
    renderPanel();
    syncButtons();
  }
  function jump(index) {
    if (index < 0 || index >= stack.length || index === cursor) return;
    cursor = index;
    restore(stack[cursor]);
    renderPanel();
    syncButtons();
  }

  function syncButtons() {
    const u = document.getElementById('btnUndo');
    const r = document.getElementById('btnRedo');
    if (u) u.disabled = cursor <= 0;
    if (r) r.disabled = cursor >= stack.length - 1;
  }

  function renderPanel() {
    const panel = document.getElementById('historyPanel');
    if (!panel || panel.hidden) return;
    const items = stack.map((snap, i) => `
      <li data-idx="${i}" class="${i === cursor ? 'cur' : ''}" role="button" tabindex="0">
        <span class="idx num">${i + 1}</span><span>${CNC.esc(snap.label)}</span>
      </li>`).join('');
    const templates = (state._editTemplates || []).map((t) => `
      <li data-tpl="${t.id}" role="button" tabindex="0">
        <span>📄 ${CNC.esc(t.name)}</span>
        <span class="dims">
          <button type="button" data-tpl-load="${t.id}">بارگذاری</button>
          <button type="button" data-tpl-del="${t.id}" class="danger">حذف</button>
        </span>
      </li>`).join('');
    panel.innerHTML = `
      <h2>تاریخچهٔ ویرایش</h2>
      <ol>${items || '<li style="cursor:default">هنوز ویرایشی ثبت نشده است.</li>'}</ol>
      <div class="toolbtns">
        <button type="button" id="saveTplBtn">💾 ذخیره به‌عنوان الگو…</button>
        <button type="button" id="refreshTplBtn">↻ الگوها</button>
      </div>
      <b style="display:block;font-size:11.5px;color:var(--muted);margin:10px 0 4px">الگوهای ویرایش (کارگاه مشترک)</b>
      <ul class="mlist">${templates || '<li style="cursor:default">الگویی ثبت نشده است.</li>'}</ul>
      <p class="statusline">الگو فقط فهرست ویرایش‌ها را پیش‌بارگذاری می‌کند؛ اعمال نهایی همیشه از
      پیش‌نمایش + گتهٔ برخورد + گتهٔ دیالکت همان سرور می‌گذرد.</p>
    `;
    panel.querySelectorAll('ol li[data-idx]').forEach((li) => {
      const go = () => jump(Number(li.dataset.idx));
      li.onclick = go;
      li.onkeydown = (e) => { if (e.key === 'Enter') go(); };
    });
    const saveBtn = panel.querySelector('#saveTplBtn');
    if (saveBtn) saveBtn.onclick = saveTemplate;
    const refreshBtn = panel.querySelector('#refreshTplBtn');
    if (refreshBtn) refreshBtn.onclick = loadTemplates;
    panel.querySelectorAll('[data-tpl-load]').forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        applyTemplate(Number(btn.dataset.tplLoad));
      };
    });
    panel.querySelectorAll('[data-tpl-del]').forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm('این الگو حذف شود؟')) return;
        try {
          await CNC.api.deleteEditTemplate(Number(btn.dataset.tplDel));
          await loadTemplates();
        } catch (err) { CNC.alert('err', CNC.esc(err.message)); }
      };
    });
  }
  CNC.renderHistoryPanel = renderPanel;

  async function loadTemplates() {
    try {
      const data = await CNC.api.editTemplates();
      state._editTemplates = data.templates || [];
      renderPanel();
    } catch (err) {
      state._editTemplates = [];
      renderPanel();
    }
  }

  async function saveTemplate() {
    const payload = CNC.opsPayload();
    if (payload.kind === 'none') {
      CNC.alert('warn', 'ویرایشی برای ذخیره نیست؛ اول روی نقشه چیزی را ویرایش کنید.');
      return;
    }
    const name = prompt('نام الگو (مثلاً «کشش قطعهٔ t.16»):');
    if (!name || !name.trim()) return;
    try {
      const res = await CNC.api.saveEditTemplate(name.trim(), payload.edits,
          `ذخیره از رابط نقشه‌محور — ${state.filename} (${payload.kind})`);
      CNC.alert('info', CNC.esc(res.message || 'الگو ذخیره شد.'));
      await loadTemplates();
    } catch (err) { CNC.alert('err', CNC.esc(err.message)); }
  }

  async function applyTemplate(id) {
    const tpl = (state._editTemplates || []).find((t) => t.id === id);
    if (!tpl) return;
    if (!state.text) {
      CNC.alert('warn', 'اول فایل مقصد را باز کنید، بعد الگو را بارگذاری کنید.');
      return;
    }
    // الگو پیش‌بارگذاری می‌شود؛ اعمال نهایی از همان پیش‌نمایش fail-closed می‌گذرد
    const edits = Array.isArray(tpl.payload) ? tpl.payload : [];
    const partEdits = edits.filter((e) => ['move_part', 'stretch_part', 'set_gap', 'distribute'].includes(e.op));
    const featureEdits = edits.filter((e) => ['array', 'grid', 'mirror', 'rotate'].includes(e.op));
    state.partOp = new Map();
    state.featureOps = [];
    for (const op of partEdits) {
      const key = op.part_b || op.part_id;
      if (key) state.partOp.set(key, op);
    }
    if (partEdits.length) {
      state.featureOps = [];       // ترکیب در یک درخواست ممنوع است
      if (featureEdits.length) {
        CNC.alert('warn', 'این قالب هم ویرایش قطعه‌ای دارد هم ویژگی‌محور؛ سرور ترکیب‌شان را در یک درخواست '
          + 'نمی‌پذیرد — فقط بخش قطعه‌ای بارگذاری شد.');
      }
    } else {
      state.featureOps = featureEdits;
    }
    push(`الگوی «${tpl.name}»`);
    CNC.requestPreview();
    CNC.alert('info', `الگوی «${CNC.esc(tpl.name)}» بارگذاری شد؛ نتیجه از پیش‌نمایش سرور می‌آید.`);
  }

  // ------------------------------------------------_رویدادها
  CNC.on('ops-changed', (detail) => {
    push((detail && detail.label) || 'ویرایش');
  });
  CNC.on('undo-request', undo);
  CNC.on('redo-request', redo);
  CNC.on('file', () => {
    // پایهٔ تاریخچه = فایل اصلی بدون ویرایش؛ undo تا همین‌جا برمی‌گردد
    stack.splice(0);
    stack.push(snapshot('فایل اصلی'));
    cursor = 0;
    syncButtons();
    renderPanel();
  });
  CNC.on('preview', () => { if (!document.getElementById('historyPanel').hidden) renderPanel(); });

  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    const key = e.key.toLowerCase();
    if (key === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
    else if ((key === 'z' && e.shiftKey) || key === 'y') { e.preventDefault(); redo(); }
  });

  // پنل تاریخچه: باز شدن → بارگذاری الگوها
  CNC.on('panel-opened', ({ id }) => {
    if (id === 'historyPanel') loadTemplates();
  });
})();
