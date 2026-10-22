/* ============================================================
   code-view.js — نمای کد با همگامی دوسویه نقشه↔کد (پرامپت ۲، فاز «ج»)
   * هر خط کد ↔ مسیرهای همان سطر روی نقشه (data-line روی pathها)
   * کلیک خط → هایلایت مسیر + شمارهٔ سطر؛ کلیک/هاور مسیر → هایلایت سطر
   * «فقط خطوط تغییرکرده»: تفاوت raw_line بین IR اصلی و IR جاری —
     یعنی همان سطرهایی که serializer وصلهٔ مختصاتی می‌زند + درج‌های آرایه
   ============================================================ */
(() => {
  'use strict';
  const CNC = window.CNC;
  const state = CNC.state;
  const $ = (id) => document.getElementById(id);

  let open = false;
  let highlightLine = null;    // سطر هایلایت‌شده از سمت نقشه

  function setOpen(next) {
    open = next;
    $('codeDrawer').hidden = !open;
    $('btnCode').classList.toggle('on', open);
    if (open) render();
    else clearMapHl();
  }
  CNC.toggleCodeView = () => setOpen(!open);

  CNC.on('code-toggle', () => CNC.toggleCodeView());
  CNC.on('code-open', () => setOpen(true));

  // ------------------------------------------------_سطرهای تغییرکرده
  function changedLines() {
    // serializer فقط سطرهای حرکتِ تغییرکرده را وصله می‌زند؛ «تغییرکرده» یعنی
    // مختصات end همان حرکت بین IR اصلی و IR جاری فرق کرده باشد (raw_line
    // در program_ir همیشه متن اصلی است — وصله در serializer اتفاق می‌افتد).
    const before = new Map();   // move id -> [x, y]
    ((state.sourceIR && state.sourceIR.blocks) || []).forEach((b) => {
      (b.motions || []).forEach((m) => {
        const e = m.end || {};
        before.set(m.id, [e.x, e.y]);
      });
    });
    const changed = new Set();
    ((state.currentIR && state.currentIR.blocks) || []).forEach((b) => {
      (b.motions || []).forEach((m) => {
        if (!before.has(m.id) || m.source_line == null) return;
        const [bx, by] = before.get(m.id);
        const e = m.end || {};
        if ((bx != null && Math.abs((e.x ?? bx) - bx) > 1e-6)
            || (by != null && Math.abs((e.y ?? by) - by) > 1e-6)) {
          changed.add(m.source_line);
        }
      });
    });
    // درج‌های آرایه/گرید: بلوک‌های تازهٔ بعدی → لنگر درج = سطر پایانی بلوک مبدأ
    const known = new Set(((state.sourceIR && state.sourceIR.blocks) || []).map((b) => b.id));
    const anchors = new Set();
    ((state.currentIR && state.currentIR.blocks) || []).forEach((b) => {
      if (!known.has(b.id)) {
        // بلوک کپی — مبدأ را از copyMap (تطبیق هندسی) بیابیم
        const srcId = state._copyMap && state._copyMap.get(b.id);
        const src = srcId && ((state.sourceIR.blocks || []).find((x) => x.id === srcId));
        if (src && src.ended_at_line != null) anchors.add(src.ended_at_line);
      }
    });
    return { changed, anchors };
  }

  // ------------------------------------------------_رندر جدول کد
  function render() {
    const tbody = $('codeTbody');
    const hint = $('codeHint');
    tbody.replaceChildren();
    if (!state.text) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.textContent = 'فایلی باز نیست.';
      tr.appendChild(td); tbody.appendChild(tr);
      hint.textContent = '';
      return;
    }
    const { changed, anchors } = changedLines();
    const lines = state.text.split(/\r?\n/);
    const only = $('onlyChanged').checked;
    const frag = document.createDocumentFragment();
    let shown = 0;
    for (let i = 0; i < lines.length && shown < 6000; i++) {
      const lineNo = i + 1;
      const isChanged = changed.has(lineNo);
      const isAnchor = anchors.has(lineNo);
      if (only && !isChanged && !isAnchor) continue;
      const tr = document.createElement('tr');
      tr.dataset.line = lineNo;
      if (isChanged) tr.className = 'ch';
      if (lineNo === highlightLine) tr.classList.add('hl');
      const a = document.createElement('td');
      a.className = 'ln'; a.textContent = lineNo;
      a.title = 'کلیک: مسیر همین سطر روی نقشه';
      const b = document.createElement('td');
      b.textContent = isAnchor ? lines[i] + '   ⟵ کپی‌های آرایه زیر این سطر درج می‌شوند' : lines[i];
      tr.append(a, b);
      frag.appendChild(tr);
      shown++;
    }
    tbody.appendChild(frag);
    const total = lines.length;
    hint.textContent = `${total} سطر · ${changed.size} سطر تغییر می‌کند`
      + (anchors.size ? ` · ${anchors.size} لنگر درج` : '');
  }
  CNC.renderCodeView = render;

  $('onlyChanged').onchange = render;
  $('btnCodeClose').onclick = () => setOpen(false);

  // ------------------------------------------------_کد → نقشه
  $('codeTbody').addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-line]');
    if (!tr) return;
    const lineNo = Number(tr.dataset.line);
    clearMapHl();
    let bboxAcc = null;
    document.querySelectorAll(`#svg path[data-line="${lineNo}"]`).forEach((p) => {
      p.classList.add('code-hl');
      const block = CNC.model.blocks.get(p.dataset.block);
      if (block && block.bbox) {
        bboxAcc = bboxAcc ? merge(bboxAcc, block.bbox) : { ...block.bbox };
      }
    });
    if (bboxAcc) CNC.zoomToBBox(bboxAcc);
    CNC.showTransient({ x: window.innerWidth / 2, y: 70 },
      `سطر <span class="num">${lineNo}</span>${partOwnerText(lineNo)}`);
  });
  function merge(a, b) {
    return {
      x_min: Math.min(a.x_min, b.x_min), x_max: Math.max(a.x_max, b.x_max),
      y_min: Math.min(a.y_min, b.y_min), y_max: Math.max(a.y_max, b.y_max),
    };
  }
  function partOwnerText(lineNo) {
    const det = state.detection;
    if (!det) return '';
    for (const p of det.parts) {
      for (const pass of p.passes || []) {
        if (lineNo >= (pass.source_lines?.[0] ?? -1) && lineNo <= (pass.source_lines?.[1] ?? -1)) {
          return ` — متعلق به ${CNC.partName(p)}`;
        }
      }
    }
    return '';
  }

  function clearMapHl() {
    document.querySelectorAll('#svg .code-hl').forEach((p) => p.classList.remove('code-hl'));
  }
  CNC.clearCodeHl = clearMapHl;

  // ------------------------------------------------_نقشه → کد
  function scrollToLine(lineNo) {
    if (!open) setOpen(true);
    highlightLine = lineNo;
    render();
    const tr = document.querySelector(`#codeTbody tr[data-line="${lineNo}"]`);
    if (tr) {
      tr.scrollIntoView({ block: 'center' });
      tr.classList.add('hl');
      setTimeout(() => tr.classList.remove('hl'), 2200);
    }
  }
  CNC.scrollToCodeLine = scrollToLine;

  CNC.on('map-click', (info) => {
    if (info.geomLine) scrollToLine(info.geomLine);
  });
  CNC.on('map-hover', (info) => {
    if (info.geomLine && CNC.hideHud) {
      // شمارهٔ سطر در هاور — برچسب کوتاه مستقل اگر چیز دیگری هایلایت نیست
      CNC.emit('line-hover', { line: info.geomLine });
    }
  });

  // رندر مجدد با هر preview (سطرهای تغییرکرده تازه)
  CNC.on('preview', () => { if (open) render(); });
  CNC.on('detection', () => { if (open) render(); });
})();
