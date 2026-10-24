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
        gapOps: state.gapOps.map((op) => ({ ...op })),
        featureOps: state.featureOps.map((op) => ({ ...op })),
      },
    };
  }

  function restore(snap) {
    state.partOp = new Map(snap.ops.partOp);
    state.gapOps = (snap.ops.gapOps || []).slice();   // نسخه‌های قبلی اسنپ‌شات gapOps ندارند
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
    // قالب‌های قطعه: مقادیر فرم باز در رندرهای مجدد (push/undo) حفظ شود
    const prevForm = panel.querySelector('#ptplForm');
    const keepForm = prevForm && !prevForm.hidden
      ? {
          name: prevForm.querySelector('#ptplName').value,
          part: prevForm.querySelector('#ptplPart').value,
          note: prevForm.querySelector('#ptplNote').value,
        }
      : null;
    const ctxPart = state.taskContext && state.taskContext.part_id;
    const ptpls = orderedPartTemplates();
    const ptplItems = ptpls.map((t) => `
      <li data-ptpl="${t.id}" role="button" tabindex="0" class="${ctxPart && t.part_id === ctxPart ? 'sel' : ''}">
        <span>📐 ${CNC.esc(t.name)}<small style="display:block;color:var(--muted);font-size:11px">${CNC.esc(t.part_label)} · ${Number(t.source_feature_count) || 0} ویژگی</small></span>
        <span class="dims">
          <button type="button" data-ptpl-load="${t.id}">بارگذاری</button>
          <button type="button" data-ptpl-del="${t.id}" class="danger">حذف</button>
        </span>
      </li>`).join('');
    const partOptions = (state._partOptions || []).map((opt) => `
      <option value="${Number(opt.id)}" ${ctxPart && opt.id === ctxPart ? 'selected' : ''}>${CNC.esc(opt.label)}</option>`).join('');
    panel.innerHTML = `
      <h2>تاریخچهٔ ویرایش</h2>
      <ol>${items || '<li style="cursor:default">هنوز ویرایشی ثبت نشده است.</li>'}</ol>
      <div class="toolbtns">
        <button type="button" id="saveTplBtn">💾 ذخیره به‌عنوان الگو…</button>
        <button type="button" id="refreshTplBtn">↻ الگوها</button>
      </div>
      <b style="display:block;font-size:11.5px;color:var(--muted);margin:10px 0 4px">الگوهای ویرایش (کارگاه مشترک)</b>
      <ul class="mlist">${templates || '<li style="cursor:default">الگویی ثبت نشده است.</li>'}</ul>
      <b style="display:block;font-size:11.5px;color:var(--muted);margin:12px 0 4px">قالب‌های قطعه (پارامتریک — نقش‌های هندسی)</b>
      <ul class="mlist" id="partTplList">${ptplItems || '<li style="cursor:default">قالب قطعه‌ای ثبت نشده است.</li>'}</ul>
      <div class="toolbtns">
        <button type="button" id="savePartTplBtn">📐 ذخیره به‌عنوان قالب قطعه…</button>
        <button type="button" id="refreshPartTplBtn">↻ قالب‌های قطعه</button>
      </div>
      <div class="ptpl-form" id="ptplForm" ${keepForm ? '' : 'hidden'}>
        <label>نام قالب <input id="ptplName" maxlength="80" placeholder="مثلاً آرایهٔ پیچ گوشه"></label>
        <label>قطعهٔ مقصد <select id="ptplPart"><option value="">— انتخاب قطعه —</option>${partOptions}</select></label>
        <label>یادداشت <input id="ptplNote" maxlength="200" placeholder="اختیاری"></label>
        <div class="toolbtns">
          <button type="button" id="ptplSave" class="primary">ذخیرهٔ قالب قطعه</button>
          <button type="button" id="ptplCancel">انصراف</button>
        </div>
      </div>
      <p class="statusline">الگوی ویرایش فهرست ویرایش‌ها را پیش‌بارگذاری می‌کند؛ قالب قطعه نقش‌های هندسی
      ویرایش‌های ویژگی‌محور (آرایه/گرید/آینه/چرخش) را به قطعهٔ مقصد می‌بندد و روی فایل جدید همان قطعه
      با تطبیق هندسی سرور بارگذاری می‌شود. اعمال نهایی هر دو همیشه از پیش‌نمایش + گتهٔ برخورد + گتهٔ
      دیالکت همان سرور می‌گذرد. برای جابه‌جایی/کشش قطعه، «الگوی ویرایش» را ذخیره کنید.</p>
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
    // قالب‌های قطعه — فاز ۲.۲
    if (keepForm) {
      const form = panel.querySelector('#ptplForm');
      if (form) {
        form.querySelector('#ptplName').value = keepForm.name;
        form.querySelector('#ptplPart').value = keepForm.part;
        form.querySelector('#ptplNote').value = keepForm.note;
      }
    }
    const savePTplBtn = panel.querySelector('#savePartTplBtn');
    if (savePTplBtn) savePTplBtn.onclick = openPartTemplateForm;
    const refreshPTplBtn = panel.querySelector('#refreshPartTplBtn');
    if (refreshPTplBtn) refreshPTplBtn.onclick = loadPartTemplates;
    const ptplSave = panel.querySelector('#ptplSave');
    if (ptplSave) ptplSave.onclick = savePartTemplate;
    const ptplCancel = panel.querySelector('#ptplCancel');
    if (ptplCancel) ptplCancel.onclick = () => togglePartTemplateForm(false);
    panel.querySelectorAll('[data-ptpl-load]').forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        applyPartTemplate(Number(btn.dataset.ptplLoad));
      };
    });
    panel.querySelectorAll('[data-ptpl-del]').forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm('این قالب قطعه حذف شود؟')) return;
        try {
          await CNC.api.deletePartTemplate(Number(btn.dataset.ptplDel));
          await loadPartTemplates();
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
    // گارد روی دادهٔ واقعی، نه برچسب kind — «چیزی برای ذخیره هست؟» یعنی
    // edits غیرخالی؛ اگر معناشناسی kind روزی تغییر کند گارد نمی‌شکند.
    if (!Array.isArray(payload.edits) || !payload.edits.length) {
      CNC.alert('warn', 'ویرایشی برای ذخیره نیست؛ اول روی نقشه چیزی را ویرایش کنید.');
      return;
    }
    const name = prompt('نام الگو (مثلاً «کشش قطعهٔ t.16»):');
    if (!name || !name.trim()) return;
    try {
      const res = await CNC.api.saveEditTemplate(name.trim(), payload.edits,
          `ذخیره از رابط نقشه‌محور — ${state.filename} (${payload.kind})`);
      CNC.alert('info', CNC.esc(res.message || 'الگو ذخیره شد.'), null, 5000);
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
    const partEdits = edits.filter((e) => ['move_part', 'stretch_part'].includes(e.op));
    const gapEdits = edits.filter((e) => ['set_gap', 'distribute', 'stretch_chain'].includes(e.op));
    const featureEdits = edits.filter((e) => ['array', 'grid', 'mirror', 'rotate'].includes(e.op));
    state.partOp = new Map();
    state.gapOps = [];      // بارگذاری الگو جایگزین کامل وضعیت است — باگ شناور نماند
    state.featureOps = [];
    // bugfix: distribute به چند قطعه تعلق دارد و برای هر قطعه نباید تکرار شود؛
    // قبلاً کلیدش (part_b || part_id) پیدا نمی‌شد و بی‌صدا دور ریخته می‌شد.
    for (const op of partEdits) state.partOp.set(op.part_id, op);
    if (gapEdits.length) state.gapOps = gapEdits;
    if (partEdits.length || gapEdits.length) {
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
    CNC.alert('info', `الگوی «${CNC.esc(tpl.name)}» بارگذاری شد؛ نتیجه از پیش‌نمایش سرور می‌آید.`, null, 5000);
  }

  // ------------------------------------------------_قالب‌های قطعه (پارامتریک — فاز ۲.۲)
  // بستن حلقهٔ صف دستی: ثبت قالب از ابزار CAD + بارگذاری روی فایل جدید همان
  // قطعه با تطبیق هندسی سرور. اعمال نهایی همیشه از پیش‌نمایش fail-closed
  // همان سرور می‌گذرد — قالب فقط ویرایش‌ها را پیش‌بارگذاری می‌کند.
  async function loadPartTemplates() {
    try {
      const data = await CNC.api.partTemplates();
      state._partTemplates = data.templates || [];
      state._partOptions = data.part_options || [];
    } catch (err) {
      state._partTemplates = [];
      CNC.alert('err', 'بارگذاری قالب‌های قطعه ناموفق بود: ' + CNC.esc(err.message));
    }
    renderPanel();
  }

  // قالب‌های قطعهٔ زمینهٔ فعلی (deep-link تسک) اول — بقیه بر اساس تازگی.
  function orderedPartTemplates() {
    const ctxPart = state.taskContext && state.taskContext.part_id;
    const list = (state._partTemplates || []).slice();
    list.sort((a, b) => (
      ((b.part_id === ctxPart) - (a.part_id === ctxPart))
      || String(b.updated_at || '').localeCompare(String(a.updated_at || ''))
    ));
    return list;
  }

  function togglePartTemplateForm(show) {
    const form = document.getElementById('ptplForm');
    if (!form) return;
    form.hidden = !show;
    if (show) {
      const name = form.querySelector('#ptplName');
      const part = form.querySelector('#ptplPart');
      if (name && !name.value) name.focus();
      // پیش‌انتخاب قطعهٔ زمینهٔ فعلی (deep-link صف دستی)
      const ctxPart = state.taskContext && state.taskContext.part_id;
      if (part && ctxPart) part.value = String(ctxPart);
    }
  }

  async function openPartTemplateForm() {
    if (!state.text) {
      CNC.alert('warn', 'اول فایل مقصد را باز کنید، بعد قالب قطعه را ذخیره کنید.');
      return;
    }
    const payload = CNC.opsPayload();
    if (!Array.isArray(payload.edits) || !payload.edits.length) {
      CNC.alert('warn', 'ویرایشی برای ذخیره نیست؛ اول روی نقشه ویرایش ویژگی‌محور (آرایه/گرید/آینه/چرخش) ثبت کنید.');
      return;
    }
    // صداقت: قالب قطعه فقط ویرایش ویژگی‌محور می‌پذیرد (قرارداد سرور). اگر
    // ویرایش قطعه‌ای فعال است، صریح بگوییم چه چیزی ذخیره نمی‌شود.
    if (state.partOp.size || state.gapOps.length) {
      const ok = confirm('قالب قطعه فقط ویرایش‌های ویژگی‌محور (آرایه/گرید/آینه/چرخش) را نگه می‌دارد؛ '
        + 'ویرایش‌های قطعه‌ای فعال (جابه‌جایی/کشش/فاصله) در آن ذخیره نمی‌شوند — '
        + 'برای آن‌ها «ذخیره به‌عنوان الگو» را استفاده کنید. با ذخیرهٔ فقط بخش ویژگی‌محور ادامه می‌دهید؟');
      if (!ok) return;
    }
    await loadPartTemplates();
    togglePartTemplateForm(true);
  }

  async function savePartTemplate() {
    const form = document.getElementById('ptplForm');
    const nameEl = form && form.querySelector('#ptplName');
    const partEl = form && form.querySelector('#ptplPart');
    const noteEl = form && form.querySelector('#ptplNote');
    const name = nameEl ? nameEl.value.trim() : '';
    if (!name) { CNC.alert('warn', 'نام قالب قطعه لازم است.'); return; }
    const partId = partEl ? Number(partEl.value || 0) : 0;
    if (!partId) { CNC.alert('warn', 'قطعهٔ مقصد قالب را انتخاب کنید.'); return; }
    try {
      const res = await CNC.api.savePartTemplate({
        name,
        part_id: partId,
        note: noteEl ? noteEl.value.trim() : '',
        edits: state.featureOps.slice(),
        content: state.text,
        filename: state.filename,
      });
      togglePartTemplateForm(false);
      await loadPartTemplates();
      const queueLink = CNC.urls.manualQueue || '/cnc/manual/';
      CNC.alert('info', CNC.esc(res.message || 'قالب قطعه ذخیره شد.')
        + ` — اگر این قطعه در <a href="${CNC.esc(queueLink)}">صف دستی CNC</a> است، سیگنالش همین حالا زنده می‌شود.`,
        'ptpl-save');
    } catch (err) { CNC.alert('err', CNC.esc(err.message), 'ptpl-save'); }
  }

  async function applyPartTemplate(id) {
    const tpl = (state._partTemplates || []).find((t) => t.id === id);
    if (!tpl) return;
    if (!state.text) {
      CNC.alert('warn', 'اول فایل مقصد را باز کنید، بعد قالب قطعه را بارگذاری کنید.');
      return;
    }
    if ((state.partOp.size || state.gapOps.length)
        && !confirm('قالب قطعه وضعیت ویرایش فعلی (شامل ویرایش‌های قطعه‌ای) را کامل جایگزین می‌کند. ادامه می‌دهید؟')) {
      return;
    }
    try {
      const res = await CNC.api.applyPartTemplate(id, state.text, state.filename);
      const concrete = Array.isArray(res.edits) ? res.edits : [];
      state.partOp = new Map();
      state.gapOps = [];
      state.featureOps = concrete;
      push(`قالب قطعهٔ «${tpl.name}»`);
      CNC.requestPreview();
      const matched = Array.isArray(res.mapping) ? res.mapping.length : 0;
      CNC.alert('info', `قالب قطعهٔ «${CNC.esc(tpl.name)}» بارگذاری شد (${matched} نقش هندسی منطبق)؛ نتیجه از پیش‌نمایش سرور می‌آید.`, null, 5000);
    } catch (err) { CNC.alert('err', CNC.esc(err.message)); }
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

  // پنل تاریخچه: باز شدن → بارگذاری الگوها + قالب‌های قطعه (فاز ۲.۲)
  CNC.on('panel-opened', ({ id }) => {
    if (id === 'historyPanel') { loadTemplates(); loadPartTemplates(); }
  });
})();
