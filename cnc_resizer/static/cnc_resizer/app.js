/* ============================================================
   app.js — چسب برنامه (پرامپت ۲): نوار ابزار شناور، بازرس، پنل‌ها،
   گتهٔ پست‌پروسسور، هشدارها با aria-live، ابزار اندازه‌گیری و boot.
   هیچ <select> و جدول فاصله‌ای وجود ندارد؛ نقشه خودِ رابط است.
   ============================================================ */
(() => {
  'use strict';
  const CNC = window.CNC;
  const state = CNC.state;
  const $ = (id) => document.getElementById(id);
  const esc = CNC.esc;

  // ------------------------------------------------_دسترسی به URLها و ستاپ
  CNC.urls = window.__CNC_URLS__ || {};
  try { CNC.setup = JSON.parse($('cncSetupData').textContent || 'null'); } catch (_) { CNC.setup = null; }

  // ------------------------------------------------_هشدارها + aria-live
  const alerts = new Map();  // key -> {el}
  function alertBox(kind, html, key) {
    const box = $('alertBox');
    if (key && alerts.has(key)) alerts.get(key).el.remove();
    const el = document.createElement('div');
    el.className = `banner ${kind}`;
    el.innerHTML = html + ' <button type="button" class="x" aria-label="بستن">×</button>';
    el.querySelector('.x').onclick = () => { el.remove(); alerts.delete(key); };
    box.appendChild(el);
    if (key) alerts.set(key, { el });
    if (kind === 'err') ariaLive(html, 'assertive'); else ariaLive(html, 'polite');
    return el;
  }
  function clearAlert(key) {
    if (!key) { $('alertBox').replaceChildren(); alerts.clear(); return; }
    const a = alerts.get(key);
    if (a) { a.el.remove(); alerts.delete(key); }
  }
  function ariaLive(html, politeness) {
    const el = politeness === 'assertive' ? $('ariaLiveAssertive') : $('ariaLive');
    if (el) el.textContent = html.replace(/<[^>]+>/g, ' ');
  }
  CNC.alert = alertBox;
  CNC.clearAlert = clearAlert;
  CNC.ariaLive = ariaLive;

  // ------------------------------------------------_بازرس
  function inspectorFile() {
    const det = state.detection;
    const parts = (det && det.parts) || [];
    const ops = (det && det.operations) || [];
    const meta = state.fileMeta || {};
    const rows = [
      ['فایل', `<span class="num">${esc(state.filename || '—')}</span>`],
      ['قطعه‌ها', `<b class="num">${parts.length}</b>`],
      ['عملیات باز', `<b class="num">${ops.length}</b>`],
      ['ورق', meta.sheet_bbox
        ? `<span class="num">${CNC.f1(meta.sheet_bbox.width)}×${CNC.f1(meta.sheet_bbox.height)}</span> mm` : '—'],
      ['ضخامت', meta.sheet_thickness != null ? `<span class="num">${CNC.f1(meta.sheet_thickness)}</span> mm` : 'نامشخص'],
    ];
    return `
      <h2>نقشهٔ CNC</h2>
      <dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      ${parts.length || ops.length
        ? `<p class="insp-empty" style="margin-top:8px">روی هر قطعه یا عملیات کلیک کنید تا بازرس همین‌جا شرح آن را بیاورد. Shift+کلیک چندانتخابی و کشیدن روی فضای خالی، کادر انتخاب می‌سازد.</p>`
        : ''}
      ${(det && det.infos || []).length
        ? `<div class="insp-info">ℹ ${det.infos.map(esc).join('<br>ℹ ')}</div>` : ''}
      ${(det && det.warnings || []).length
        ? `<div class="insp-warn">⚠ ${det.warnings.map(esc).join('<br>⚠ ')}</div>` : ''}
      ${!det || (!parts.length && !ops.length)
        ? `<div class="insp-warn">${state.text
            ? 'هندسهٔ برش قابل‌تشخیصی در این فایل پیدا نشد؛ هیچ قطعه یا عملیاتی ثبت نشد.'
            : 'فایلی بارگذاری نشده است.'}</div>` : ''}
      ${det && !parts.length && ops.length
        ? `<div class="insp-warn">قطعه‌ای شناسایی نشد — فقط عملیات برش باز دیده می‌شود. این فایل برای ویرایش «قطعه‌ای» مناسب نیست؛ عملیات‌ها فقط نمایش داده می‌شوند و دلیلش همین است.</div>` : ''}
    `;
  }

  function capRow(ok, label, reason) {
    return `<li><span class="${ok ? 'yes' : 'no'}">${ok ? '✓' : '✗'}</span>
            <span><b>${label}</b>${reason ? ` — ${esc(reason)}` : ''}</span></li>`;
  }

  function stretchReason(part, axis) {
    if (part.locked) return 'کانتورهای قطعه روی هم افتاده‌اند و کل ویرایشش قفل است.';
    if (!part[`stretch_band_${axis}`]) {
      return 'نوار صاف به‌اندازهٔ کافی بلند در این محور وجود ندارد (قوس یا لبهٔ مایل کشیده می‌شود و خراب می‌شود).';
    }
    return null;
  }

  function inspectorPart(part) {
    const b = part.bbox;
    const after = state._afterPairs && state._afterPairs.get(part.id);
    const dims = after ? after.bbox : b;
    const caps = part.capabilities || {};
    const fixed = state.fixedParts.has(part.id);
    const excluded = state.outputsExcluded.has(part.id);
    const capHtml = [
      capRow(caps.can_translate && !part.locked, 'جابه‌جایی (درگ)',
             part.locked ? 'کانتورهای قطعه واقعاً روی هم افتاده‌اند؛ ویرایش قفل است.' : ''),
      capRow(!!caps.can_stretch_x && !fixed, 'کشش عرض (X)', stretchReason(part, 'x')
             || (fixed ? 'این قطعه «ثابت» علامت خورده؛ از منوی راست‌کلیک کشیدنی کنید.' : '')),
      capRow(!!caps.can_stretch_y && !fixed, 'کشش ارتفاع (Y)', stretchReason(part, 'y')
             || (fixed ? 'این قطعه «ثابت» علامت خورده؛ از منوی راست‌کلیک کشیدنی کنید.' : '')),
      capRow(!!caps.can_rotate90 && !part.locked, 'چرخش ۹۰°',
             part.locked ? 'قفل هم‌پوشانی.' : ''),
      capRow(!!caps.can_mirror && !part.locked, 'آینه', part.locked ? 'قفل هم‌پوشانی.' : ''),
    ].join('');
    const children = (part.children || []).map((c) => `
      <li data-child="${esc(c.id)}" role="button" tabindex="0">
        <span>${esc(CNC.KIND_FA[c.kind] || c.kind)}</span>
        <span class="dims num">${CNC.f1(c.bbox.x_max - c.bbox.x_min)}×${CNC.f1(c.bbox.y_max - c.bbox.y_min)}</span>
      </li>`).join('');
    const op = state.partOp.get(part.id);
    const opText = op ? describeOp(op) : null;
    return `
      <h2><span class="badge" style="background:${CNC.partColor(part)}"></span>
          ${esc(CNC.partName(part))} — ${esc(CNC.SHAPE_FA[part.shape_kind] || part.shape_kind)}</h2>
      <dl class="kv">
        <dt>ابعاد کانتور</dt><dd class="num">${CNC.f1(dims.x_max - dims.x_min)}×${CNC.f1(dims.y_max - dims.y_min)} mm</dd>
        <dt>عمق‌ها</dt><dd class="num">${(part.depths || []).map((v) => CNC.f1(v)).join('، ') || '—'} mm</dd>
        <dt>پاس‌ها</dt><dd class="num">${(part.passes || []).length}</dd>
        <dt>ابزار</dt><dd class="num">${part.tool != null ? 'T' + part.tool : '—'}</dd>
        <dt>مساحت</dt><dd class="num">${CNC.f1(part.area_path)} mm²</dd>
      </dl>
      ${opText ? `<div class="insp-warn" style="background:var(--accent-soft);color:var(--accent);border-color:var(--accent)">
        ویرایش ثبت‌شده: ${opText}</div>` : ''}
      <ul class="caps">${capHtml}</ul>
      ${children ? `<b style="font-size:12px;color:var(--muted)">فرزندها / عملیات داخل قطعه</b>
        <ul class="children">${children}</ul>` : ''}
      ${(part.warnings || []).length ? `<div class="insp-warn">⚠ ${part.warnings.map(esc).join('<br>⚠ ')}</div>` : ''}
      ${excluded ? `<div class="insp-warn">این قطعه از خروجی حذف شده است. «حذف از خروجی» با وصلهٔ مختصاتیِ امن قابل بیان نیست؛
        تا وقتی فعال است دانلود مسدود می‌شود تا فایل ناقص تحویل دستگاه نشود.</div>` : ''}
      <div class="actions">
        <button type="button" data-act="zoom">🔍 زوم</button>
        <button type="button" data-act="toggle-fixed">${fixed ? '🔓 کشیدنی کن' : '🔒 ثابت کن'}</button>
        <button type="button" data-act="toggle-exclude" class="danger">${excluded ? '↩ برگرداندن به خروجی' : '🚫 حذف از خروجی'}</button>
        ${op ? '<button type="button" data-act="reset-op">↩ بازگردانی ویرایش این قطعه</button>' : ''}
      </div>
    `;
  }

  function describeOp(op) {
    if (op.op === 'move_part') {
      return `جابه‌جایی (<span class="num">${CNC.sgn(op.dx)}, ${CNC.sgn(op.dy)}</span>) mm`;
    }
    if (op.op === 'stretch_part') {
      const dist = op.distribution === 'gaps' ? ' <span style="color:var(--muted)">(توزیع بین سوراخ‌ها)</span>' : '';
      return op.new_size != null
        ? `کشش ${op.axis === 'y' ? 'ارتفاع' : 'عرض'} به <span class="num">${CNC.f1(op.new_size)}</span> mm${dist}`
        : `کشش ${op.axis === 'y' ? 'ارتفاع' : 'عرض'} <span class="num">${CNC.sgn(op.delta)}</span> mm${dist}`;
    }
    if (op.op === 'set_gap') {
      const other = ((state.detection && state.detection.parts) || []).find((p) => p.id === op.part_a);
      return `فاصله با ${esc(other ? CNC.partName(other) : op.part_a)} = <span class="num">${CNC.f1(op.gap)}</span> mm (${op.axis.toUpperCase()})`;
    }
    return esc(op.op);
  }
  CNC.describeOp = describeOp;

  function inspectorOp(op) {
    const owner = op.assigned_part
      ? (((state.detection.parts) || []).find((p) => p.id === op.assigned_part)) : null;
    return `
      <h2>${esc(CNC.KIND_FA[op.kind] || op.kind)} <span class="num" style="color:var(--muted)">${esc(op.id)}</span></h2>
      <dl class="kv">
        <dt>ابعاد</dt><dd class="num">${CNC.f1(op.bbox.x_max - op.bbox.x_min)}×${CNC.f1(op.bbox.y_max - op.bbox.y_min)} mm</dd>
        <dt>عمق‌ها</dt><dd class="num">${(op.passes || []).map((p) => CNC.f1(p.z)).join('، ') || '—'} mm</dd>
        <dt>قطعهٔ مالک</dt><dd>${owner ? esc(CNC.partName(owner)) : 'ندارد — عملیات مستقل'}</dd>
      </dl>
      ${owner ? '' : `<div class="insp-warn">این عملیات به هیچ قطعه‌ای تعلق ندارد؛ با ویرایش قطعه‌ای جابه‌جا نمی‌شود
        و دلیلش این است که حذف/جابه‌جایی مسیر مستقل باید از فایل منبع انجام شود.</div>`}
      <div class="actions"><button type="button" data-act="zoom">🔍 زوم</button></div>
    `;
  }

  function renderInspector() {
    const box = $('inspector');
    box.hidden = false;
    const sel = state.selection;
    const part = sel.find((s) => s.type === 'part');
    const op = sel.find((s) => s.type === 'op');
    if (part) {
      const p = ((state.detection && state.detection.parts) || []).find((x) => x.id === part.id);
      box.innerHTML = p ? inspectorPart(p) : inspectorFile();
    } else if (op) {
      const o = ((state.detection && state.detection.operations) || []).find((x) => x.id === op.id);
      box.innerHTML = o ? inspectorOp(o) : inspectorFile();
    } else {
      box.innerHTML = inspectorFile();
    }
    const zoomBtn = box.querySelector('[data-act="zoom"]');
    if (zoomBtn) zoomBtn.onclick = () => {
      const target = part || op;
      const ref = part ? CNC.viewEls.refs[target.id] : CNC.viewEls.opEls[target.id];
      const bb = ref && ref.bb;
      if (bb) zoomToBBox(bb);
    };
    const fixBtn = box.querySelector('[data-act="toggle-fixed"]');
    if (fixBtn && part) fixBtn.onclick = () => {
      if (state.fixedParts.has(part.id)) state.fixedParts.delete(part.id);
      else state.fixedParts.add(part.id);
      renderInspector();
      CNC.emit('parts-state', {});
    };
    const exBtn = box.querySelector('[data-act="toggle-exclude"]');
    if (exBtn && part) exBtn.onclick = () => {
      if (state.outputsExcluded.has(part.id)) state.outputsExcluded.delete(part.id);
      else state.outputsExcluded.add(part.id);
      renderInspector();
      CNC.applyPartStates();
      CNC.emit('gate', { ...state.gate });
    };
    const resetBtn = box.querySelector('[data-act="reset-op"]');
    if (resetBtn && part) resetBtn.onclick = () => {
      if (CNC.resetPartOp) CNC.resetPartOp(part.id);
    };
    box.querySelectorAll('[data-child]').forEach((li) => {
      const activate = () => {
        const pid = sel.find((s) => s.type === 'part');
        const p = ((state.detection && state.detection.parts) || []).find((x) => x.id === pid.id);
        const child = p && (p.children || []).find((c) => c.id === li.dataset.child);
        if (child && child.bbox) zoomToBBox(child.bbox);
      };
      li.onclick = activate;
      li.onkeydown = (e) => { if (e.key === 'Enter') activate(); };
    });
  }
  CNC.renderInspector = renderInspector;

  function zoomToBBox(bb) {
    const w = Math.max(bb.x_max - bb.x_min, 30), h = Math.max(bb.y_max - bb.y_min, 30);
    const cx = (bb.x_min + bb.x_max) / 2, cy = (bb.y_min + bb.y_max) / 2;
    CNC.viewport.setVB({ x: cx - w * .75, y: -cy - h * .75, w: w * 1.5, h: h * 1.5 });
  }
  CNC.zoomToBBox = zoomToBBox;

  // ------------------------------------------------_پنل‌های شناور
  function togglePanel(id, btn) {
    const panel = $(id);
    const show = panel.hidden;
    ['filePanel', 'toolsPanel', 'layersPanel', 'historyPanel', 'resizePanel', 'helpPanel'].forEach((pid) => {
      const p = $(pid);
      if (p && pid !== id) p.hidden = true;
    });
    panel.hidden = !show;
    if (btn) btn.classList.toggle('on', show);
    if (show && id === 'toolsPanel') renderToolsPanel();
    if (show && id === 'layersPanel') renderLayersPanel();
    if (show && id === 'resizePanel') { syncResizeTarget(); if (CNC.fetchResizeGaps) CNC.fetchResizeGaps(); }
    CNC.emit('panel-opened', { id, open: show });
  }
  CNC.togglePanel = togglePanel;

  // -------------------------------------------_تغییر طول فایل (روش‌های محیط قدیمی)
  // بازخورد اپراتور: علاوه بر دلتا/روش، «انتخاب فاصله‌ها» هم برگشت — جدول
  // فاصله‌های بین گروه‌های سوراخ با تیکِ ثابت‌ماندن و مقدار دستی + زنجیرهٔ
  // اندازهٔ بین سوراخ‌ها روی خود نقشه، مثل رابط کاربری قبلی.
  function currentFileLength() {
    // محور طول فایل CNC همان Y است (قرارداد پروفایل قطعه) — ارتفاع کادر ورق.
    const bbox = (state.fileMeta && state.fileMeta.sheet_bbox) || {};
    return bbox.height != null ? Number(bbox.height) : null;
  }
  function syncResizeTarget() {
    const deltaIn = $('resizeDelta'), targetIn = $('resizeTarget');
    if (!deltaIn || !targetIn) return;
    const cur = currentFileLength();
    if (cur == null) { targetIn.value = ''; targetIn.disabled = true; return; }
    targetIn.disabled = false;
    const delta = Number(deltaIn.value);
    if (Number.isFinite(delta)) targetIn.value = Math.round((cur + delta) * 10) / 10;
  }
  function resizeStatus(kind, html) {
    const box = $('resizeResult');
    box.hidden = false;
    box.innerHTML = `<div class="banner ${kind}">${html}</div>`;
  }

  // فاصله‌های بین گروه‌های سوراخ (features/gaps موتور قدیمی) — هم جدول پنل و
  // هم زنجیرهٔ اندازهٔ روی نقشه از همین پاسخ تغذیه می‌شود (delta=0 = فقط خواندن).
  async function fetchResizeGaps() {
    const st = CNC.state;
    if (!st.text) return;
    const seq = ++st.resizeSeq;
    try {
      const res = await CNC.api.resize(st.text, st.filename,
                                       { mode: 'equal', delta: 0, symmetric: true });
      if (seq !== st.resizeSeq) return;
      if (!res.resizable || !(res.gaps || []).length) {
        st.resizeGaps = null; st.resizeLengths = null;
        renderGapTable();
        return;
      }
      st.resizeGaps = res.gaps.map((g) => ({ ...g, _enabled: true }));
      st.resizeLengths = { before: res.length_before, after: res.length_after };
      renderGapTable();
      CNC.emit('rerender', {});   // زنجیرهٔ اندازه روی نقشه دوباره رسم شود
    } catch (err) {
      if (seq !== st.resizeSeq) return;
      st.resizeGaps = null; st.resizeLengths = null;
      renderGapTable(err.message);
    }
  }
  CNC.fetchResizeGaps = fetchResizeGaps;

  function renderGapTable(errMsg) {
    const table = $('gapTable'), tbody = $('gapTableBody'), hint = $('gapHint');
    if (!table || !tbody) return;
    const gaps = state.resizeGaps;
    const mode = (document.querySelector('input[name="resizeMode"]:checked') || {}).value || 'equal';
    if (errMsg) {
      table.hidden = true; hint.hidden = false;
      hint.textContent = `فاصله‌های بین گروه‌ها خوانده نشد: ${errMsg}`;
      return;
    }
    if (!gaps || !gaps.length) {
      table.hidden = true; hint.hidden = false;
      hint.textContent = 'این فایل فاصلهٔ قابل‌توزیعی ندارد (تغییر طول خودکار در دسترس نیست) — ویرایش قطعه‌ای روی خود نقشه همچنان کار می‌کند.';
      return;
    }
    if (mode === 'kerf') {
      table.hidden = true; hint.hidden = false;
      hint.textContent = 'در حالت kerf توزیع کاملاً خودکار است (گام شیار ثابت می‌ماند)؛ جدول فاصله‌ها فقط برای «مساوی/متناسب/دستی» معنا دارد.';
      return;
    }
    table.hidden = false; hint.hidden = false;
    hint.textContent = 'تیک فاصله را بردارید تا اندازه‌اش ثابت بماند؛ با بردن ماوس روی هر ردیف، همان ناحیه روی نقشه هایلایت می‌شود.';
    const manual = mode === 'manual';
    tbody.replaceChildren();
    gaps.forEach((g, k) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><input type="checkbox" class="gap-en" ${g._enabled === false ? '' : 'checked'}
                        aria-label="فاصلهٔ ${k + 1} تغییر کند"></td>`
        + `<td class="n">${CNC.f1(g.y0)}…${CNC.f1(g.y1)}</td>`
        + `<td class="n">${CNC.f1(g.length)}</td>`
        + (manual
          ? `<td class="n"><input type="number" class="gap-man" step="0.1"
               value="${CNC.f1(g.increment || 0)}" aria-label="تغییر دستی فاصلهٔ ${k + 1}"> </td>`
          : `<td class="n">${g.increment ? CNC.sgn(g.increment) : '—'}</td>`)
        + `<td class="n">${CNC.f1((g.length || 0) + (g.increment || 0))}</td>`;
      tbody.appendChild(tr);
      tr.addEventListener('mouseenter', () => CNC.viewport.gapHighlight(g.y0, g.y1));
      tr.addEventListener('mouseleave', () => CNC.viewport.gapHighlight(null, null));
      tr.querySelector('.gap-en').addEventListener('change', (e) => { g._enabled = e.target.checked; });
      const man = tr.querySelector('.gap-man');
      if (man) man.addEventListener('input', (e) => { g._manual = Number(e.target.value); });
    });
  }

  function renderResizeResult(res, mode) {
    if (res.ok === false) { resizeStatus('err', esc(res.error || 'تغییر طول ناموفق بود.')); return; }
    const warnings = (res.warnings || []).map(esc).join('<br>• ');
    if (res.resizable === false) {
      resizeStatus('warn', 'این فایل قابل تغییر طول خودکار نیست.<br>• ' + (warnings || 'دلیل در پیام‌ها آمده است.'));
      return;
    }
    // جدول/زنجیره با نتیجهٔ واقعی به‌روز شود (در حالت kerf فاصله‌ای برنمی‌گردد)
    if (res.gaps && res.gaps.length) {
      state.resizeGaps = res.gaps.map((g, k) => ({
        ...g, _enabled: !!(state.resizeGaps && state.resizeGaps[k]
          ? state.resizeGaps[k]._enabled : g.enabled),
      }));
      state.resizeLengths = { before: res.length_before, after: res.length_after };
      renderGapTable();
      CNC.emit('rerender', {});
    }
    const before = res.length_before != null ? CNC.f1(res.length_before) : '—';
    const after = res.length_after != null ? CNC.f1(res.length_after) : '—';
    const modeName = { equal: 'مساوی بین گروه‌ها', proportional: 'متناسب با فاصله‌ها', kerf: 'خم‌کاری kerf', manual: 'دستی' }[mode] || mode;
    resizeStatus('ok',
      `تغییر طول با روش «${esc(modeName)}» محاسبه شد: طول <span class="num">${before}</span> → <span class="num">${after}</span> mm`
      + (warnings ? `<br>• ${warnings}` : '')
      + '<div class="toolbtns" style="margin-top:8px"><button type="button" id="resizeReplace" class="primary">جایگزینی فایل روی نقشه با نسخهٔ جدید</button></div>');
    const replaceBtn = $('resizeReplace');
    if (replaceBtn) replaceBtn.onclick = async () => {
      if (!res.cnc_text || !String(res.cnc_text).trim()) { resizeStatus('err', 'متن نتیجه خالی است.'); return; }
      if (CNC.hasOps() && !confirm('ویرایش‌های فعلی نقشه و تاریخچهٔ واگرد با جایگزینی فایل پاک می‌شوند. ادامه می‌دهید؟')) return;
      const baseName = (state.filename || 'part.cnc').replace(/\.[^.]+$/, '');
      await CNC.loadText(`${baseName}_L${CNC.f1(res.length_after ?? 0)}.cnc`, res.cnc_text);
      // بازخورد اپراتور: فایل جایگزین‌شدهٔ پنل طول بدون ویرایش نقشه هم از
      // «دانلود G-code» (گتهٔ دیالکت) قابل دانلود است.
      state.resizedFromPanel = true;
      CNC.renderGate();
      resizeStatus('ok', 'فایل جایگزین شد و نقشه دوباره شناسایی شد. فایل دستگاه همچنان فقط از مسیر «دانلود G-code» (گتهٔ دیالکت کنترلر) صادر می‌شود.');
    };
  }
  function bindResizePanel() {
    const deltaIn = $('resizeDelta'), targetIn = $('resizeTarget');
    deltaIn.oninput = () => {
      const cur = currentFileLength();
      if (cur != null && Number.isFinite(Number(deltaIn.value))) {
        targetIn.value = Math.round((cur + Number(deltaIn.value)) * 10) / 10;
      }
    };
    targetIn.oninput = () => {
      const cur = currentFileLength();
      const t = Number(targetIn.value);
      if (cur != null && Number.isFinite(t)) deltaIn.value = Math.round((t - cur) * 10) / 10;
    };
    $('resizeChips').querySelectorAll('button[data-d]').forEach((btn) => {
      btn.onclick = () => { deltaIn.value = btn.dataset.d; syncResizeTarget(); };
    });
    // تغییر روش: ستون «تغییر» بین نمایشی/دستی عوض می‌شود؛ kerf جدول را جمع می‌کند
    const modes = $('resizeModes');
    if (modes) modes.addEventListener('change', () => renderGapTable());
    $('resizeApply').onclick = async () => {
      const mode = (document.querySelector('input[name="resizeMode"]:checked') || {}).value || 'equal';
      const delta = Number(deltaIn.value);
      if (mode !== 'manual' && (!Number.isFinite(delta) || delta === 0)) {
        resizeStatus('err', 'ابتدا میزان تغییر (Δ) را وارد کنید — مثلاً ۱۰+ یا ۱۰−.');
        return;
      }
      if (!state.text) { resizeStatus('err', 'اول یک فایل CNC باز کنید.'); return; }
      const params = { mode, delta, symmetric: $('resizeSym').checked };
      // بازخورد اپراتور: انتخاب فاصله‌ها مثل روش قدیمی — تیک هر فاصله و
      // در حالت دستی مقدار هر کدام به همان پارامترهای engine قدیمی می‌رود.
      if (state.resizeGaps && state.resizeGaps.length && mode !== 'kerf') {
        params.enabled = state.resizeGaps.map((g) => g._enabled !== false);
        if (mode === 'manual') {
          params.manual = state.resizeGaps.map((g) => (Number.isFinite(g._manual) ? g._manual : 0));
        }
      }
      const btn = $('resizeApply');
      btn.disabled = true;
      try {
        const res = await CNC.api.resize(state.text, state.filename, params);
        renderResizeResult(res, mode);
      } catch (err) {
        resizeStatus('err', esc(err.message));
      } finally { btn.disabled = false; }
    };
  }

  // فایل
  function renderFilePanel() {
    const list = $('archiveList');
    if (!list) return;
    list.replaceChildren();
    (state.archiveFiles || []).forEach((item) => {
      const li = document.createElement('li');
      li.setAttribute('role', 'button'); li.tabIndex = 0;
      li.innerHTML = `<b>${esc(item.name)}</b>
        <span class="dims">${item.length ? CNC.f1(item.length) + ' mm' : ''}${item.resizable ? '' : ' · فقط‌نمایش'}</span>`;
      const activate = async () => {
        try {
          const data = await CNC.api.archiveFile(item.id);
          CNC.loadText(data.name + '.cnc', data.content);
          $('dropTitle').textContent = 'فایل آرشیو انتخاب شد';
          $('fname').textContent = data.name + '.cnc';
          list.querySelectorAll('li').forEach((n) => n.classList.remove('sel'));
          li.classList.add('sel');
        } catch (err) { alertBox('err', esc(err.message)); }
      };
      li.onclick = activate;
      li.onkeydown = (e) => { if (e.key === 'Enter') activate(); };
      list.appendChild(li);
    });
    if (!(state.archiveFiles || []).length) {
      list.innerHTML = '<li style="cursor:default">آرشیو خالی است یا در دسترس نیست.</li>';
    }
  }
  CNC.renderFilePanel = renderFilePanel;

  // ابزار
  function renderToolsPanel() {
    const body = $('toolTableBody');
    if (!body) return;
    body.replaceChildren();
    (state.toolSlots || []).forEach((s) => {
      const tr = document.createElement('tr');
      const t1 = document.createElement('td'); t1.textContent = 'T' + s.slot;
      const t2 = document.createElement('td');
      const name = document.createElement('input'); name.type = 'text'; name.value = s.label || '';
      name.dataset.slot = s.slot; name.dataset.field = 'label'; name.setAttribute('aria-label', `نام ابزار T${s.slot}`);
      const t3 = document.createElement('td');
      const dia = document.createElement('input'); dia.type = 'number'; dia.min = '0.1'; dia.step = '0.1';
      dia.value = (s.dia == null ? '' : s.dia); dia.placeholder = '—';
      dia.dataset.slot = s.slot; dia.dataset.field = 'dia'; dia.setAttribute('aria-label', `قطر ابزار T${s.slot}`);
      t2.appendChild(name); t3.appendChild(dia); tr.append(t1, t2, t3); body.appendChild(tr);
    });
    const sets = $('toolSetList');
    sets.replaceChildren();
    (state.toolSets || []).forEach((name) => {
      const li = document.createElement('li');
      li.innerHTML = `<b>${esc(name)}</b>
        <span class="toolbtns" style="margin:0">
          <button type="button" data-a="load">بارگذاری</button>
          <button type="button" data-a="del" class="danger">حذف</button>
        </span>`;
      li.querySelector('[data-a=load]').onclick = async () => {
        try { const d = await CNC.api.toolSet('load', name); await loadTools(); toolStatus(d.message); } catch (e) { toolStatus(e.message); }
      };
      li.querySelector('[data-a=del]').onclick = async () => {
        if (!confirm(`ست «${name}» حذف شود؟`)) return;
        try { const d = await CNC.api.toolSet('delete', name); await loadTools(); toolStatus(d.message); } catch (e) { toolStatus(e.message); }
      };
      sets.appendChild(li);
    });
  }
  function toolStatus(msg) { const el = $('toolStatus'); if (el) el.textContent = msg || ''; }

  function collectToolRows() {
    const rows = [];
    document.querySelectorAll('#toolTableBody [data-field]').forEach((inp) => {
      const slot = Number(inp.dataset.slot);
      let row = rows.find((r) => r.slot === slot);
      if (!row) { row = { slot }; rows.push(row); }
      row[inp.dataset.field] = inp.value;
    });
    return rows;
  }

  async function loadTools() {
    try {
      const data = await CNC.api.tools();
      state.toolSlots = data.slots || []; state.toolSets = data.sets || [];
      state.toolMap = {};
      state.toolSlots.forEach((s) => { if (s.dia != null) state.toolMap[s.slot] = s.dia; });
      renderToolsPanel();
    } catch (err) { toolStatus(err.message); }
  }
  CNC.loadTools = loadTools;

  // لایه‌ها
  function renderLayersPanel() {
    const model = CNC.model;
    const det = state.detection;
    const box = $('layersPanel');
    if (!model || !model.blocks.size) { box.innerHTML = '<h2>لایه‌ها</h2><p class="insp-empty">فایلی باز نیست.</p>'; return; }
    const depthChips = model.depthLevels.map((z, i) => {
      const key = String(z); const on = state.layers.depth.get(key) !== false;
      return `<button type="button" class="chip ${on ? '' : 'off'}" data-lay="depth" data-key="${key}">
        <span class="sw" style="background:${DEPTH_COLORS[i % DEPTH_COLORS.length]}"></span>Z${CNC.f1(z)}</button>`;
    }).join('');
    const toolChips = model.tools.map((t) => {
      const key = String(t); const on = state.layers.tool.get(key) !== false;
      const dia = state.toolMap[t];
      return `<button type="button" class="chip ${on ? '' : 'off'}" data-lay="tool" data-key="${key}">
        <span class="sw" style="background:#64748b"></span>T${t}${dia != null ? ` · ${CNC.f1(dia)}mm` : ''}</button>`;
    }).join('');
    const kindCount = {};
    ((det && det.operations) || []).forEach((op) => { kindCount[op.kind] = (kindCount[op.kind] || 0) + 1; });
    const kindChips = Object.entries(kindCount).map(([kind, n]) => `
      <span class="num">${n}× ${esc(CNC.KIND_FA[kind] || kind)}</span>`).join(' · ');
    box.innerHTML = `
      <h2>لایه‌ها</h2>
      <div class="layrow"><b>عمق (Z) — رنگ هر عمق ثابت است و راهنمای کنار نقشه همین chips هستند</b>
        <div class="chips">${depthChips || '<span class="insp-empty">عمقی ثبت نشده</span>'}</div></div>
      <div class="layrow"><b>ابزار</b><div class="chips">${toolChips || '<span class="insp-empty">—</span>'}</div></div>
      <div class="layrow"><b>انواع عملیات باز</b><div class="laylegend">${kindChips || '—'}</div></div>
      <label class="laycheck"><input type="checkbox" id="layGhost" ${state.layers.showGhost ? 'checked' : ''}> شبح «قبل»</label>
      <label class="laycheck"><input type="checkbox" id="layOps" ${state.layers.showOps ? 'checked' : ''}> عملیات باز</label>
      <label class="laycheck"><input type="checkbox" id="layGrid" ${state.layers.showGrid ? 'checked' : ''}> شبکه</label>
      <label class="laycheck"><input type="checkbox" id="layFix" ${state._showFix !== false ? 'checked' : ''}> نقاط میخ/فیکس</label>
      <label class="laycheck"><input type="checkbox" id="layDims" ${state.layers.showDims ? 'checked' : ''}> خطوط اندازه</label>
    `;
    box.querySelectorAll('[data-lay]').forEach((btn) => {
      btn.onclick = () => {
        const { lay, key } = btn.dataset;
        const store = state.layers[lay];
        store.set(key, store.get(key) === false);
        btn.classList.toggle('off', store.get(key) === false);
        CNC.applyLayerVisibility();
      };
    });
    $('layGhost').onchange = (e) => { state.layers.showGhost = e.target.checked; CNC.emit('rerender', {}); };
    $('layOps').onchange = (e) => { state.layers.showOps = e.target.checked; CNC.emit('rerender', {}); };
    $('layGrid').onchange = (e) => { state.layers.showGrid = e.target.checked; CNC.emit('rerender', {}); };
    $('layFix').onchange = (e) => { state._showFix = e.target.checked; CNC.toggleFixtures(e.target.checked); };
    $('layDims').onchange = (e) => { state.layers.showDims = e.target.checked; CNC.emit('rerender', {}); };
  }
  const DEPTH_COLORS = ['#1d6ce0', '#157a45', '#7c3aed', '#d97706', '#0e7490',
                        '#be185d', '#4d7c0f', '#b45309', '#4338ca', '#334155'];

  // ------------------------------------------------_گیت + دانلود
  function renderGate() {
    const chip = $('gateChip');
    const btn = $('btnDownload');
    const g = state.gate;
    const excluded = state.outputsExcluded.size > 0;
    const passthrough = !g.edits && state.resizedFromPanel;   // فایل جایگزین‌شدهٔ پنل «طول»
    chip.classList.remove('ok', 'blocked');
    let text, title;
    if (!state.text) { text = 'بدون فایل'; title = 'اول فایل بارگذاری کنید.'; }
    else if (!g.edits && !passthrough) { text = 'بدون ویرایش'; title = 'ویرایشی ثبت نشده؛ خروجی همان فایل ورودی می‌شد.'; }
    else if (passthrough) { text = 'فایل تغییر-طول‌یافته ✓'; title = 'این فایل حاصل «تغییر طول» پنل طول است؛ دانلود از همان گتهٔ دیالکت عبور می‌کند.'; chip.classList.add('ok'); }
    else if (excluded) { text = `دانلود قفل — ${state.outputsExcluded.size} قطعه از خروجی حذف شده`; title = 'حذف قطعه از خروجی با وصلهٔ مختصاتی امن قابل بیان نیست؛ برای دانلود، حذف را برگردانید.'; }
    else if (g.allowed === true) { text = 'safe_for_execution ✓'; title = 'گتهٔ دیالکت کنترلر پاس شد؛ G-code دستگاه آماده است.'; chip.classList.add('ok'); }
    else { text = 'safe_for_execution ✗'; title = (g.blockers || []).join('\n') || 'خروجی مسدود است.'; chip.classList.add('blocked'); }
    chip.querySelector('.txt').textContent = text;
    chip.title = title;
    btn.disabled = !(state.text && (g.edits || passthrough)
                     && (passthrough || g.allowed === true) && !excluded);
    btn.title = title;
  }
  CNC.renderGate = renderGate;

  async function downloadOutput() {
    const payload = CNC.opsPayload();
    // بازخورد اپراتور: بعد از «جایگزینی فایل روی نقشه» (پنل طول/روش قدیمی)
    // ویرایشی روی نقشه نیست ولی فایل همان حاصل تغییر طول است — عبور مستقیم
    // از گتهٔ دیالکت، نه ردِ «ویرایشی انجام ندادی».
    if (payload.kind === 'none') {
      if (!state.resizedFromPanel) return;
      await downloadPassthrough();
      return;
    }
    try {
      const res = await CNC.api.output(state.text, state.filename, payload.edits);
      if (!res.allowed) {
        alertBox('err', 'خروجی مسدود شد: ' + esc((res.blockers || []).join(' — ')));
        return;
      }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([res.cnc_text], { type: 'text/plain' }));
      a.download = (state.filename || 'part').replace(/\.[^.]+$/, '') + '_edited.cnc';
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
      CNC.emit('output', res);
    } catch (err) {
      alertBox('err', esc(err.message));
    }
  }

  async function downloadPassthrough() {
    try {
      const res = await CNC.api.output(state.text, state.filename, []);
      if (!res.allowed) {
        alertBox('err', 'خروجی مسدود شد: ' + esc((res.blockers || []).join(' — ')));
        return;
      }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([res.cnc_text], { type: 'text/plain' }));
      a.download = (state.filename || 'part').replace(/\.[^.]+$/, '') + '_resized.cnc';
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
      CNC.emit('output', res);
    } catch (err) {
      alertBox('err', esc(err.message));
    }
  }
  CNC.downloadOutput = downloadOutput;

  // ------------------------------------------------_ابزار اندازه‌گیری
  function snapPoints() {
    const pts = [];
    CNC.model.blocks.forEach((b) => {
      for (const s of b.segments) {
        pts.push([s.x1, s.y1], [s.x2, s.y2]);
        if (s.t === 'arc') pts.push([(s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2]);
      }
    });
    return pts;
  }
  function snap(world, tolPx) {
    const px = CNC.viewport.pxPerMM();
    const tol = (tolPx || 14) / px;
    let best = null, bd = tol;
    for (const [x, y] of state._snapPts || []) {
      const d = Math.hypot(x - world.x, y - world.y);
      if (d < bd) { bd = d; best = { x, y }; }
    }
    return best || { x: world.x, y: world.y };
  }

  function setTool(tool) {
    state.tool = tool;
    $('btnSelect').classList.toggle('on', tool === 'select');
    $('btnMeasure').classList.toggle('on', tool === 'measure');
    CNC.viewport.svg.classList.toggle('tool-measure', tool === 'measure');
    state._measureA = null; state._measureLive = null;
    CNC.drawMeasures();
    CNC.hideHud();
  }
  CNC.setTool = setTool;

  CNC.on('measure-click', (info) => {
    const p = snap(info.world);
    if (!state._measureA) {
      state._measureA = p;
    } else {
      state.measures.push({ ax: state._measureA.x, ay: state._measureA.y, bx: p.x, by: p.y });
      state._measureA = null; state._measureLive = null;
      CNC.drawMeasures();
    }
  });
  CNC.on('map-hover', (info) => {
    if (state.tool !== 'measure' || !CNC.model) return;
    const p = snap(info.world);
    CNC.hideHud();
    if (state._measureA) {
      state._measureLive = { a: state._measureA, b: p };
      const L = Math.hypot(p.x - state._measureA.x, p.y - state._measureA.y);
      CNC.alert('info', `اندازه‌گیری: نقطهٔ دوم را کلیک کنید — <span class="num">${CNC.f2(L)} mm</span> · Esc لغو`, 'measure');
      CNC.drawMeasures();
    }
  });

  // ------------------------------------------------_رویدادهای سراسری
  CNC.on('file', () => {
    clearAlert();
    CNC.model = null;          // هندسهٔ فایل قبلی نباید باقی بماند
    state.needFit = true;      // نمای تازه روی فایل تازه
    $('btnDownload').disabled = true;
    CNC.emit('rerender', {});
  });
  CNC.on('api-error', ({ source, error }) => {
    // هیچ‌وقت صفحهٔ خالی خطا نمی‌دهیم — نقشه و پیام روی همان‌جا
    alertBox('err', `خواندن ${source === 'parts' ? 'تشخیص قطعه‌ها' : 'هندسهٔ فایل'} ناموفق بود: ${esc(error.message)} — می‌توانید فایل دیگری باز کنید یا دوباره تلاش کنید.`);
    CNC.emit('rerender', {});
  });
  CNC.on('source', () => {
    // مدل هندسه از IR جاری ساخته می‌شود
    state._ghostModel = null;
    CNC.model = CNC.buildBlockModel(state.currentIR || state.sourceIR);
    state._snapPts = snapPoints();
    CNC.emit('rerender', {});
  });
  CNC.on('detection', () => {
    renderInspector();
    CNC.emit('rerender', {});
    CNC.renderGate();
    // جدول فاصله‌ها + زنجیرهٔ اندازهٔ بین سوراخ‌ها مثل محیط قدیمی بعد از هر
    // شناسایی به‌روز می‌شود (delta=0 فقط خواندنی است؛ خرابی‌ها بی‌سروصدا جمع می‌شوند)
    if (CNC.fetchResizeGaps) CNC.fetchResizeGaps();
  });
  CNC.on('selection', () => { renderInspector(); });
  CNC.on('gate', () => { renderGate(); });
  CNC.on('ops-changed', () => { renderInspector(); });
  CNC.on('preview', () => { renderInspector(); });
  CNC.on('rerender', () => {
    if (CNC.viewport && CNC.viewport.svg) CNC.viewport.render();
  });
  CNC.on('parts-state', () => { /* edit-tools/hook آینده */ });

  // ------------------------------------------------_رویدادهای UI
  function bindUI() {
    $('btnSelect').onclick = () => setTool('select');
    $('btnMeasure').onclick = () => setTool('measure');
    $('btnUndo').onclick = () => CNC.emit('undo-request', {});
    $('btnRedo').onclick = () => CNC.emit('redo-request', {});
    $('btnFile').onclick = (e) => togglePanel('filePanel', e.currentTarget);
    $('btnTools').onclick = (e) => togglePanel('toolsPanel', e.currentTarget);
    $('btnLayers').onclick = (e) => togglePanel('layersPanel', e.currentTarget);
    $('btnHistory').onclick = (e) => togglePanel('historyPanel', e.currentTarget);
    $('btnCode').onclick = () => CNC.emit('code-toggle', {});
    $('btnResize').onclick = (e) => {
      togglePanel('resizePanel', e.currentTarget);
      syncResizeTarget();
    };
    $('btnHelp').onclick = (e) => togglePanel('helpPanel', e.currentTarget);
    bindResizePanel();
    $('btnDownload').onclick = downloadOutput;
    $('gateChip').onclick = () => {
      const g = state.gate;
      if (g.allowed === true) { alertBox('info', 'گتهٔ خروجی پاس شد: ' + esc(g.warnings?.join(' — ') || 'دیالکت کنترلر تأیید شد.'), 'gate'); return; }
      alertBox('err', 'دلایل مسدودبودن خروجی: <br>• ' + (g.blockers || []).map(esc).join('<br>• '), 'gate');
    };

    // فایل
    const drop = $('drop'), fileIn = $('file');
    drop.onclick = () => fileIn.click();
    drop.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileIn.click(); } };
    ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', (e) => { if (e.dataTransfer.files[0]) loadLocalFile(e.dataTransfer.files[0]); });
    fileIn.onchange = () => { if (fileIn.files[0]) loadLocalFile(fileIn.files[0]); };
    function loadLocalFile(f) {
      if (f.size > 2 * 1024 * 1024) { alertBox('err', 'فایل بیش از ۲ مگابایت است'); return; }
      const r = new FileReader();
      r.onload = () => {
        CNC.loadText(f.name, String(r.result));
        $('dropTitle').textContent = 'فایل انتخاب شد';
        $('fname').textContent = f.name;
      };
      r.onerror = () => alertBox('err', 'خواندن فایل ممکن نشد');
      r.readAsText(f);
    }

    // ابزار
    $('saveToolsBtn').onclick = async () => {
      try {
        const d = await CNC.api.saveTools(collectToolRows());
        toolStatus(d.message);
        await loadTools();
        // bugfix: قبلاً loadText کامل صدا زده می‌شد که همهٔ partOp/featureOps
        // و تاریخچهٔ واگرد را پاک می‌کرد؛ حالا فقط تشخیص با میز ابزار تازه
        // دوباره گرفته می‌شود و ویرایش‌ها دست‌نخورده می‌مانند.
        await CNC.redetect();
      } catch (err) { toolStatus(err.message); }
    };
    $('saveToolSetBtn').onclick = async () => {
      const name = prompt('نام ست ابزار (مثلاً «ست قطعه A»):');
      if (!name || !name.trim()) return;
      try { const d = await CNC.api.toolSet('save', name.trim()); await loadTools(); toolStatus(d.message); }
      catch (err) { toolStatus(err.message); }
    };

    // قیدهای دستگاه (پاکت) — ذخیرهٔ محلی + بازبینی گته
    const ML_FIELDS = [['machineXMin', 'x_min'], ['machineXMax', 'x_max'],
                       ['machineYMin', 'y_min'], ['machineYMax', 'y_max']];
    try {
      const saved = JSON.parse(localStorage.getItem('cncMachineLimits') || '{}');
      ML_FIELDS.forEach(([id, key]) => {
        const inp = $(id);
        if (inp && saved[key] != null) inp.value = saved[key];
        inp.onchange = () => {
          const limits = {};
          ML_FIELDS.forEach(([fid, fkey]) => {
            const v = Number($(fid).value);
            if ($(fid).value !== '' && Number.isFinite(v)) limits[fkey] = v;
          });
          localStorage.setItem('cncMachineLimits', JSON.stringify(limits));
          CNC.refreshGate();
        };
      });
    } catch (_) { /* localStorage در دسترس نیست — قیدها دستی می‌مانند */ }

    // زوم
    $('zin').onclick = () => CNC.viewport.zoomAt(0.7);
    $('zout').onclick = () => CNC.viewport.zoomAt(1.4);
    $('zfit').onclick = () => CNC.viewport.fit();
  }

  // ------------------------------------------------_میانبرهای سراسری
  document.addEventListener('keydown', (e) => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
    if (e.key === 'Escape') {
      setTool('select'); state._measureA = null; state._measureLive = null;
      CNC.drawMeasures(); clearAlert('measure');
      CNC.emit('edit-cancel', {});
    } else if (e.key === 'm' || e.key === 'M' || e.key === 'م') {
      setTool(state.tool === 'measure' ? 'select' : 'measure');
    } else if (e.key === 'f' || e.key === 'F') {
      CNC.viewport.fit();
    } else if (e.key === 'g' || e.key === 'G') {
      state.layers.showGrid = !state.layers.showGrid; CNC.emit('rerender', {});
    }
  });

  // ------------------------------------------------_لینک عمیق از صف ورق‌ها
  // ?task_id= / ?part_id=  → فایل فعلیِ همان قطعه (همان مسیر حل برنامه‌ریزی)
  // ?sheet_id=             → برنامهٔ کل ورق چیدمان‌شده (فقط خروجی تأییدشده)
  function setContextBar({ title, dims, file, warn, register }) {
    const bar = $('taskContextBar');
    if (!bar) return;
    $('tcTitle').textContent = title || '—';
    const dimsEl = $('tcDims');
    if (dims) { dimsEl.textContent = dims; dimsEl.hidden = false; } else { dimsEl.hidden = true; }
    const fileEl = $('tcFile');
    if (file) { fileEl.textContent = file; fileEl.hidden = false; } else { fileEl.hidden = true; }
    $('tcStatus').hidden = !warn;
    $('tcRegister').hidden = !register;
    const back = $('tcBack');
    if (back) back.href = CNC.urls.sheetPlans || '/cnc/sheets/';
    bar.hidden = false;
  }

  function bindTaskContext() {
    const button = $('tcRegister');
    if (!button) return;
    button.addEventListener('click', async () => {
      const taskId = button.dataset.taskId;
      if (!taskId || !state.text) return;
      button.disabled = true;
      $('tcStatusLine').textContent = 'در حال اعتبارسنجی دیالکت و هندسه…';
      try {
        const res = await CNC.api.registerManualFix(taskId, state.text);
        $('tcStatusLine').textContent = res.message || 'فایل دستی ثبت شد.';
        $('tcStatus').hidden = true;
        button.hidden = true;
        alertBox('info', esc(res.message || 'فایل دستی ثبت و تأیید شد؛ به ایستگاه برش برگردید و برنامهٔ CNC را دوباره بسازید.'), 'manual-fix');
      } catch (err) {
        $('tcStatusLine').textContent = '';
        alertBox('err', 'ثبت فایل دستی رد شد: ' + esc(err.message), 'manual-fix');
      } finally {
        button.disabled = false;
      }
    });
  }

  async function openTaskContext(params) {
    try {
      const data = await CNC.api.taskFile(params);
      CNC.loadText(data.filename, data.content);
      $('dropTitle').textContent = 'فایل از ایستگاه برش باز شد';
      $('fname').textContent = data.filename;
      const meta = data.meta || {};
      const kindLabel = meta.file_kind === 'derived'
        ? (meta.file_mode === 'manual' ? 'فایل دستی ثبت‌شدهٔ اپراتور' : 'فایل مشتق با الگوی تأییدشده')
        : 'فایل پایهٔ آرشیو';
      setContextBar({
        title: `تسک #${meta.task_id} — ${meta.part_name || meta.barcode || 'قطعه'}`,
        dims: (meta.effective_length && meta.effective_width)
          ? `طول مؤثر ${CNC.f1(parseFloat(meta.effective_length))} × عرض ${CNC.f1(parseFloat(meta.effective_width))} mm`
          : '',
        file: kindLabel,
        warn: !!meta.manual_required,
        register: !!meta.manual_required,
      });
      const button = $('tcRegister');
      if (button) button.dataset.taskId = meta.task_id || '';
      if (!meta.resolvable && meta.failure_reason) {
        alertBox('info',
          'تولید خودکار فایل برای طول جدید این قطعه مسدود است: ' + esc(meta.failure_reason)
          + '<br>فایل پایه باز شد؛ آن را در همین نقشه اصلاح کنید و با دکمهٔ «ثبت این فایل به‌عنوان اصلاح دستی» به صف دستی بدهید.',
          'task-file');
      }
    } catch (err) {
      alertBox('err', 'بازکردن فایل تسک ناموفق بود: ' + esc(err.message), 'task-file');
    }
  }

  async function openSheetContext(planId) {
    try {
      const data = await CNC.api.sheetProgram(planId);
      CNC.loadText(data.filename, data.content);
      $('dropTitle').textContent = 'برنامهٔ کامل ورق باز شد';
      $('fname').textContent = data.filename;
      const meta = data.meta || {};
      setContextBar({
        title: `کل ورق CNC-${String(planId).padStart(4, '0')}`,
        dims: (meta.width && meta.height)
          ? `${CNC.f1(parseFloat(meta.width))} × ${CNC.f1(parseFloat(meta.height))} mm · ${meta.pieces} قطعه`
          : '',
        file: meta.material || '',
      });
    } catch (err) {
      alertBox('err', 'بازکردن برنامهٔ ورق ناموفق بود: ' + esc(err.message), 'sheet-view');
    }
  }

  // ------------------------------------------------_boot
  async function boot() {
    CNC.viewport.init($('svg'));
    bindUI();
    setTool('select');
    renderInspector();
    renderGate();
    await loadTools();
    try {
      const data = await CNC.api.archiveList();
      state.archiveFiles = data.files || [];
      renderFilePanel();
    } catch (_) { state.archiveFiles = []; renderFilePanel(); }
    bindTaskContext();
    const params = new URLSearchParams(location.search);
    const sheetId = Number(params.get('sheet_id') || 0);
    const taskId = Number(params.get('task_id') || 0);
    const partId = Number(params.get('part_id') || 0);
    if (sheetId) await openSheetContext(sheetId);
    else if (taskId) await openTaskContext({ task_id: taskId });
    else if (partId) await openTaskContext({ part_id: partId });
  }
  document.addEventListener('DOMContentLoaded', boot);
})();
