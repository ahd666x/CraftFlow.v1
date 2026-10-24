/* ============================================================
   sheet_preview.js — پیش‌نمایش صادقانهٔ مسیرهای ابزار ورق CNC
   (نقشهٔ راه فاز ۱.۲) داده‌ها فقط از برنامهٔ ذخیره‌شدهٔ تأییدشدهٔ
   همان ورق می‌آید (api/sheet-preview/<id>)؛ هیچ چیز بازسازی
   نمی‌شود. برش = خط توپر به رنگ ابزار، سریع = خط‌چین خاکستری،
   حرکت سریع داخل متریال (Z<=0) = قرمز برجسته.
   ============================================================ */
(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const TOOL_COLORS = ['#2563eb', '#d97706', '#16a34a', '#7c3aed', '#0284c7',
    '#be185d', '#4d7c0f', '#b45309', '#4338ca', '#0f766e'];
  const modalEl = document.getElementById('paths-modal');
  if (!modalEl) return;

  const svg = document.getElementById('paths-svg');
  const titleEl = document.getElementById('paths-title');
  const subtitleEl = document.getElementById('paths-subtitle');
  const statsEl = document.getElementById('paths-stats');
  const alertEl = document.getElementById('paths-alert');
  const rapidToggle = document.getElementById('paths-toggle-rapid');
  let currentUrl = null;
  let loading = false;

  const el = (name, attrs, parent) => {
    const node = document.createElementNS(NS, name);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    if (parent) parent.appendChild(node);
    return node;
  };

  function render(data) {
    svg.replaceChildren();
    const w = data.sheet.width, h = data.sheet.height;
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.classList.remove('hide-rapid');
    if (rapidToggle) rapidToggle.checked = false;
    const fontPx = Math.max(w, h) * 0.013;

    // گروه چرخیده: مختصات CNC (Y رو به بالا) مستقیم استفاده می‌شود
    const flip = el('g', { transform: `translate(0 ${h}) scale(1 -1)` }, svg);
    el('rect', { x: 0, y: 0, width: w, height: h, fill: '#fffdf5',
                 stroke: '#cbd5e1', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }, flip);

    const palette = new Map((data.tools || []).map((tool, index) => [tool, TOOL_COLORS[index % TOOL_COLORS.length]]));
    const cutByTool = new Map();
    for (const path of data.paths || []) {
      if (path.k === 'cut') {
        const tool = path.tool || 0;
        if (!cutByTool.has(tool)) cutByTool.set(tool, []);
        cutByTool.get(tool).push(path);
      }
    }

    // جعبهٔ قطعات (زیر مسیرها)
    for (const box of data.boxes || []) {
      el('rect', { x: box.x, y: box.y, width: box.w, height: box.h, class: 'box' }, flip);
    }

    // مسیرهای سریع (خط‌چین) — خارج از گروه چرخیده هم می‌شد، همین‌جا ساده‌تر است
    for (const path of data.paths || []) {
      if (path.k !== 'rapid' || path.risk) continue;
      el('polyline', { points: path.pts.map((p) => p.join(',')).join(' '),
                       class: 'p-rapid' }, flip);
    }
    // مسیرهای برش به تفکیک ابزار
    for (const [tool, list] of cutByTool) {
      for (const path of list) {
        el('polyline', { points: path.pts.map((p) => p.join(',')).join(' '),
                         class: 'p-cut', stroke: palette.get(tool) || '#334155' }, flip);
      }
    }
    // حرکت سریع پرخطر داخل متریال — قرمز و روی همه‌چیز
    let risks = 0;
    for (const path of data.paths || []) {
      if (path.k !== 'rapid' || !path.risk) continue;
      risks += 1;
      el('polyline', { points: path.pts.map((p) => p.join(',')).join(' '),
                       class: 'p-cut p-risk', stroke: '#dc2626' }, flip);
    }

    // برچسب‌ها (متن نباید آینه شود — لایهٔ جدا با Y معکوس دستی)
    const labels = el('g', {}, svg);
    for (const box of data.boxes || []) {
      const text = el('text', { x: box.x + 4, y: h - box.y - box.h + fontPx * 1.1,
                                'font-size': fontPx, fill: '#475569',
                                'font-weight': 700 }, labels);
      text.textContent = box.label || '';
    }

    titleEl.textContent = `مسیرهای ابزار — ${data.filename || ''}`;
    subtitleEl.textContent = [data.material, 'منبع: برنامهٔ ذخیره‌شدهٔ تأییدشدهٔ همین ورق',
      data.adapter ? `پست‌پروسسور: ${data.adapter}` : ''].filter(Boolean).join(' · ');

    const stat = data.stats || {};
    const changes = (data.tool_changes || []).map((c) => `T${c.tool}`).join('، ') || '—';
    const pieces = [
      ['برش', `${stat.cut_mm} mm`],
      ['سریع', `${stat.rapid_mm} mm`],
      ['پلونج', stat.plunges],
      ['عمق‌ها', (stat.depths || []).length ? stat.depths.join('، ') + ' mm' : '—'],
      ['ابزارها', (data.tools || []).length ? data.tools.map((t) => `T${t}`).join('، ') : '—'],
      ['تعویض', changes],
      ['سطر برنامه', stat.lines],
    ];
    statsEl.innerHTML = pieces.map(([k, v]) =>
      `<span>${k} <b class="num">${v}</b></span>`).join('')
      + (risks ? `<span class="paths-risk-chip"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i> ${risks} حرکت سریع در سطح متریال (Z≤0)</span>` : '');
  }

  function showAlert(message) {
    alertEl.hidden = false;
    alertEl.textContent = message;
    svg.replaceChildren();
    statsEl.textContent = '';
    subtitleEl.textContent = '';
  }

  async function open(button) {
    const url = button.dataset.pathsUrl;
    if (!url || loading) return;
    loading = true;
    currentUrl = url;
    titleEl.textContent = 'در حال بارگذاری مسیرها…';
    subtitleEl.textContent = '';
    statsEl.textContent = '';
    alertEl.hidden = true;
    svg.replaceChildren();
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
    try {
      const response = await fetch(url, { headers: { 'Accept': 'application/json' } });
      const data = await response.json().catch(() => ({}));
      if (currentUrl !== url) return;
      if (!response.ok || !data.ok) {
        showAlert(data.error || `دریافت پیش‌نمایش ناموفق بود (کد ${response.status}).`);
      } else {
        render(data);
      }
    } catch (err) {
      if (currentUrl === url) showAlert('ارتباط با سرور برقرار نشد؛ دوباره تلاش کنید.');
    } finally {
      loading = false;
    }
  }

  document.querySelectorAll('[data-paths-open]').forEach((button) => {
    button.addEventListener('click', () => open(button));
  });
  if (rapidToggle) rapidToggle.addEventListener('change', () => {
    svg.classList.toggle('hide-rapid', rapidToggle.checked);
  });
})();
