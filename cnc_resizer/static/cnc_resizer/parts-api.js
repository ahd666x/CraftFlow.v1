/* ============================================================
   parts-api.js — وضعیت سراسری، bus رویداد و پوشش APIها (پرامپت ۲)
   هیچ منطق هندسی اینجا نیست؛ قرارداد سرور (پرامپت ۱):
     POST api/parts/            → تشخیص قطعات (فقط‌خواندنی)
     POST api/engine/analyze/   → IR برنامه (هندسهٔ قبل)
     POST api/engine/preview/   → پیش‌نمایش ویرایش‌ها (part-level یا feature-level)
     POST api/engine/preflight/ → وضعیت گتهٔ خروجی بدون تولید فایل
     POST api/engine/output/    → G-code کنترلر از مسیر دروازهٔ دیالکت
   ============================================================ */
(() => {
  'use strict';
  const CNC = (window.CNC = window.CNC || {});

  // ------------------------------------------------_bus کوچک
  const bus = new EventTarget();
  CNC.on = (type, fn) => bus.addEventListener(type, (e) => fn(e.detail));
  CNC.emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));
  CNC.$ = (sel, root) => (root || document).querySelector(sel);
  CNC.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  CNC.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ------------------------------------------------_اعداد
  CNC.f1 = (v) => (Math.round(v * 10) / 10).toFixed(1);
  CNC.f2 = (v) => (Math.round(v * 100) / 100).toFixed(2);
  CNC.sgn = (v) => (v > 0 ? '+' : v < 0 ? '−' : '') + CNC.f1(Math.abs(v));
  CNC.faDigit = (s) => String(s); // اعداد لاتین می‌مانند (الزام UI)

  // ------------------------------------------------_CSRF / fetch
  function csrf() {
    const input = document.querySelector('input[name=csrfmiddlewaretoken]');
    if (input && input.value) return input.value;
    const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  async function postJSON(url, payload) {
    const resp = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(payload || {}),
    });
    let data = null;
    try { data = await resp.json(); } catch (_) { /* پاسخ غیر JSON */ }
    if (!resp.ok) {
      const err = new Error((data && data.error) || `خطای سرور (${resp.status})`);
      err.status = resp.status; err.data = data;
      throw err;
    }
    return data;
  }
  async function getJSON(url) {
    const resp = await fetch(url, { credentials: 'same-origin' });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const err = new Error((data && data.error) || `خطای سرور (${resp.status})`);
      err.status = resp.status; err.data = data;
      throw err;
    }
    return data;
  }
  CNC.postJSON = postJSON;
  CNC.getJSON = getJSON;

  function debounce(fn, ms) {
    let t = null;
    const wrapped = (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    wrapped.cancel = () => clearTimeout(t);
    return wrapped;
  }
  CNC.debounce = debounce;

  // ------------------------------------------------_وضعیت
  // همهٔ ویرایش‌ها «مطلق نسبت به فایل اصلی» نگه‌داری می‌شوند؛ سرور هر بار
  // از روی متن اصلی، همهٔ ویرایش‌ها را یک‌جا اعمال می‌کند (قرارداد پرامپت ۱:
  // هر بلوک حداکثر یک تبدیل در هر درخواست).
  CNC.state = {
    filename: '', text: '',
    sourceIR: null,          // program dict از engine/analyze
    detection: null,         // نتیجهٔ api/parts/
    preview: null,           // نتیجهٔ engine/preview (وقتی ویرایشی هست)
    currentIR: null,         // program_ir پس از preview، وگرنه sourceIR
    partOp: new Map(),       // part_id -> {op:'move_part'|'stretch_part', ...}
    gapOps: [],              // [{op:'set_gap'|'distribute', ...}] مکمل partOp
    featureOps: [],          // فقط وقتی partOp/gapOps خالی است (array/mirror/rotate)
    outputsExcluded: new Set(),  // part_id هایی که اپراتور از خروجی حذف کرده (advisory)
    fixedParts: new Set(),       // part_id هایی که «ثابت» علامت خورده‌اند
    selection: [],           // [{type:'part'|'op'|'child', id}]
    gate: { allowed: null, blockers: [], warnings: [], edits: false },
    layers: { depth: new Map(), tool: new Map(), kind: new Map(),
              showGhost: true, showDims: true, showOps: true, showGrid: true },
    measures: [],
    toolMap: {},
    fileMeta: null,          // {sheet_bbox, sheet_bbox_finished, sheet_thickness, warnings...}
    reqSeq: 0,
  };

  CNC.hasOps = () => CNC.state.partOp.size > 0 || CNC.state.gapOps.length > 0
    || CNC.state.featureOps.length > 0;

  // فهرست ویرایش‌ها برای سرور — یا همهٔ part-level یا همهٔ feature-level.
  // هر قطعه حداکثر یک op «مطلق نسبت به فایل اصلی» (set_gap هم به‌عنوان op
  // قطعهٔ جابه‌جاشونده در همین map ذخیره می‌شود).
  CNC.opsPayload = function opsPayload() {
    const st = CNC.state;
    if (st.featureOps.length) return { edits: st.featureOps.slice(), kind: 'feature' };
    const edits = [...st.partOp.values()].map((op) => ({ ...op }));
    return { edits, kind: edits.length ? 'part' : 'none' };
  };

  // ------------------------------------------------_APIها
  CNC.machineLimits = function machineLimits() {
    // قیدهای پاکت دستگاه از پنل ابزار (ذخیرهٔ محلی) — نقدنقص صادقانه:
    // بدون پیکربندی، preflight خودش مسدود می‌کند.
    try { return JSON.parse(localStorage.getItem('cncMachineLimits') || 'null') || {}; }
    catch (_) { return {}; }
  };
  CNC.api = {
    detectParts(content, filename) {
      return postJSON(CNC.urls.parts, { content, filename });
    },
    analyze(content, filename) {
      return postJSON(CNC.urls.engineAnalyze, { content, filename });
    },
    preview(content, filename, edits) {
      return postJSON(CNC.urls.enginePreview, { content, filename, edits });
    },
    preflight(content, filename, edits) {
      return postJSON(CNC.urls.enginePreflight,
                      { content, filename, edits, machine_limits: CNC.machineLimits() });
    },
    output(content, filename, edits) {
      return postJSON(CNC.urls.engineOutput, { content, filename, edits });
    },
    archiveList() { return getJSON(CNC.urls.archive); },
    archiveFile(id) { return getJSON(`${CNC.urls.archive}?id=${encodeURIComponent(id)}`); },
    tools() { return getJSON(CNC.urls.tools); },
    saveTools(slots) { return postJSON(CNC.urls.tools, { slots }); },
    toolSet(action, name) { return postJSON(CNC.urls.toolSets, { action, name }); },
    editTemplates() { return getJSON(CNC.urls.editTemplates); },
    saveEditTemplate(name, edits, note) {
      return postJSON(CNC.urls.editTemplates, { name, edits, note });
    },
    deleteEditTemplate(id) { return postJSON(CNC.urls.editTemplateDelete.replace('0', String(id)), {}); },
  };

  // ------------------------------------------------_بارگذاری فایل
  // جریان: text → detectParts + analyze (موازی) → emit؛ سپس گیت بازبینی می‌شود.
  CNC.loadText = async function loadText(name, text) {
    const st = CNC.state;
    st.filename = name; st.text = text;
    st.sourceIR = null; st.detection = null; st.preview = null; st.currentIR = null;
    st.partOp = new Map(); st.gapOps = []; st.featureOps = [];
    st.outputsExcluded = new Set(); st.fixedParts = new Set();
    st.selection = []; st.measures = [];
    st.gate = { allowed: null, blockers: [], warnings: [], edits: false };
    st.layers.depth = new Map(); st.layers.tool = new Map(); st.layers.kind = new Map();
    CNC.emit('file', { name, text });
    const seq = ++st.reqSeq;
    const toolMap = {};
    Object.entries(st.toolMap || {}).forEach(([slot, dia]) => { toolMap[slot] = dia; });
    const [detect, analyze] = await Promise.allSettled([
      CNC.api.detectParts(text, name),
      CNC.api.analyze(text, name),
    ]);
    if (seq !== st.reqSeq) return; // فایل تازه‌تری در راه است
    if (detect.status === 'fulfilled') {
      st.detection = detect.value;
      st.fileMeta = {
        sheet_bbox: detect.value.sheet_bbox, sheet_bbox_finished: detect.value.sheet_bbox_finished,
        sheet_thickness: detect.value.sheet_thickness, warnings: detect.value.warnings || [],
      };
    } else {
      st.detection = null; st.fileMeta = null;
      CNC.emit('api-error', { source: 'parts', error: detect.reason });
    }
    if (analyze.status === 'fulfilled') {
      st.sourceIR = analyze.value.program || null;
      st.currentIR = st.sourceIR;
      if (analyze.value.analysis) st.recognition = analyze.value.analysis;
    } else {
      st.sourceIR = null; st.currentIR = null;
      CNC.emit('api-error', { source: 'analyze', error: analyze.reason });
    }
    CNC.emit('source', {});
    CNC.emit('detection', {});
    CNC.refreshGate();
  };

  // ------------------------------------------------_گیت خروجی
  CNC.refreshGate = debounce(async function refreshGate() {
    const st = CNC.state;
    if (!st.text) return;
    const payload = CNC.opsPayload();
    if (payload.kind === 'none') {
      st.gate = { allowed: null, blockers: ['ویرایشی ثبت نشده است؛ خروجی همان فایل ورودی می‌شد.'],
                  warnings: [], edits: false };
      CNC.emit('gate', { ...st.gate });
      return;
    }
    // اگر آخرین preview خطا داشته، گته هم بسته است — پیش‌فلایت اضافه نمی‌زنیم
    if (st.lastPreviewError) {
      st.gate = { allowed: false, blockers: [st.lastPreviewError], warnings: [], edits: true };
      CNC.emit('gate', { ...st.gate });
      return;
    }
    try {
      const res = await CNC.api.preflight(st.text, st.filename, payload.edits);
      const report = res.preflight || {};
      st.gate = { allowed: !!report.allowed, blockers: report.blockers || [],
                  warnings: report.warnings || [], edits: true };
    } catch (err) {
      st.gate = { allowed: false, blockers: [err.message], warnings: [], edits: true };
    }
    CNC.emit('gate', { ...st.gate });
  }, 260);
})();
