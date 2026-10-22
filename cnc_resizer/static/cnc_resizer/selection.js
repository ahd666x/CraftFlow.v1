/* ============================================================
   selection.js — انتخاب مستقیم روی نقشه (پرامپت ۲، الزام ۲)
   کلیک/Shift+کلیک/کادر کشیدن؛ هاور با هایلایت و برچسب (نوع، ابعاد،
   عمق‌ها، ابزار)؛ پیمایش Tab بین قطعات؛ همگام با بازرس.
   ============================================================ */
(() => {
  'use strict';
  const CNC = window.CNC;
  const state = CNC.state;

  let hoverEl = null;          // عنصر maphud
  let rubber = null;           // {x0,y0,x1,y1, el} به مختصات دنیا

  function sel() { return state.selection; }
  function has(sel_) { return sel().some((s) => s.type === sel_.type && s.id === sel_.id); }

  CNC.selection = {
    get() { return sel(); },
    set(list, opts) {
      state.selection = (list || []).filter((s) => s && s.id);
      paint();
      CNC.emit('selection', { selection: sel(), silent: opts && opts.silent });
    },
    toggle(sel_, additive) {
      if (has(sel_)) {
        CNC.selection.set(sel().filter((s) => !(s.type === sel_.type && s.id === sel_.id)),
                          { additive });
      } else {
        CNC.selection.set(additive ? [...sel(), sel_] : [sel_]);
      }
    },
    clear() { CNC.selection.set([]); },
    has,
  };

  // ------------------------------------------------_رنگ‌آمیزی وضعیت
  function paint() {
    document.querySelectorAll('#gParts .part-g').forEach((g) => {
      g.classList.toggle('sel', sel().some((s) => s.type === 'part' && s.id === g.dataset.part));
    });
    document.querySelectorAll('#gOps .op-g').forEach((g) => {
      g.classList.toggle('sel', sel().some((s) => s.type === 'op' && s.id === g.dataset.op));
    });
  }
  CNC.paintSelection = paint;

  // ------------------------------------------------_کلیک
  CNC.on('map-click', (info) => {
    if (state.tool === 'measure') { CNC.emit('measure-click', info); return; }
    if (info.dimId) return;   // خطوط اندازه خودش رفتار دارند (edit-tools)
    if (info.partId) {
      if (info.shift) {
        CNC.selection.toggle({ type: 'part', id: info.partId }, true);
      } else {
        // انتخاب تکْ idempotent است (کلیک دوباره، انتخاب را نمی‌زند —
        // چون همان کلیک شروع‌کنندهٔ درگ/ویرایش هم هست)
        CNC.selection.set([{ type: 'part', id: info.partId }]);
      }
    } else if (info.opId) {
      if (info.shift) {
        CNC.selection.toggle({ type: 'op', id: info.opId }, true);
      } else {
        CNC.selection.set([{ type: 'op', id: info.opId }]);
      }
    } else if (!info.shift) {
      CNC.selection.clear();
    }
  });

  // ------------------------------------------------_کادر انتخاب (rubber band)
  CNC.on('map-down', (info) => {
    if (info.panMode || info.claimed || info.button !== 0) return;
    if (!info.onEmpty || info.partId || info.opId) return;
    rubber = { x0: info.world.x, y0: info.world.y, x1: info.world.x, y1: info.world.y, el: null, shift: info.shift };
  });
  CNC.on('map-move', (info) => {
    if (!rubber || !info.down || info.down.panMode) return;
    rubber.x1 = info.world.x; rubber.y1 = info.world.y;
    if (!rubber.el) {
      rubber.el = CNC.svgEl('rect', { class: 'rubber' }, CNC.groups.gSel);
    }
    rubber.el.setAttribute('x', Math.min(rubber.x0, rubber.x1));
    rubber.el.setAttribute('y', -Math.max(rubber.y0, rubber.y1));
    rubber.el.setAttribute('width', Math.abs(rubber.x1 - rubber.x0));
    rubber.el.setAttribute('height', Math.abs(rubber.y1 - rubber.y0));
  });
  CNC.on('map-up', () => {
    if (!rubber) return;
    const r = rubber; rubber = null;
    if (r.el) r.el.remove();
    const w = Math.abs(r.x1 - r.x0), h = Math.abs(r.y1 - r.y0);
    if (w < 2 && h < 2) return;                  // کلیک ساده — توسط map-click هندل می‌شود
    const x0 = Math.min(r.x0, r.x1), x1 = Math.max(r.x0, r.x1);
    const y0 = Math.min(r.y0, r.y1), y1 = Math.max(r.y0, r.y1);
    const hits = [];
    const parts = (state.detection && state.detection.parts) || [];
    for (const part of parts) {
      const b = partBBox(part);
      if (b && b.x_min >= x0 && b.x_max <= x1 && b.y_min >= y0 && b.y_max <= y1) {
        hits.push({ type: 'part', id: part.id });
      }
    }
    const ops = (state.detection && state.detection.operations) || [];
    for (const op of ops) {
      const b = op.bbox;
      if (b && b.x_min >= x0 && b.x_max <= x1 && b.y_min >= y0 && b.y_max <= y1) {
        hits.push({ type: 'op', id: op.id });
      }
    }
    CNC.selection.set(r.shift ? [...new Map([...sel(), ...hits].map((s) => [s.type + s.id, s])).values()] : hits);
  });

  function partBBox(part) {
    const ref = CNC.viewEls.refs[part.id];
    return ref ? ref.bb : part.bbox;
  }

  // ------------------------------------------------_هاور + برچسب
  CNC.on('map-hover', (info) => {
    clearHover();
    if (state.tool === 'measure' || !CNC.model || !CNC.model.blocks.size) return;
    let text = null, el = null;
    if (info.partId) {
      const part = findPart(info.partId);
      if (part) {
        el = CNC.viewEls.refs[info.partId] && CNC.viewEls.refs[info.partId].g;
        el && el.classList.add('hov');
        text = hoverTextPart(part);
      }
    } else if (info.opId) {
      const op = findOp(info.opId);
      if (op) {
        el = CNC.viewEls.opEls[op.id] && CNC.viewEls.opEls[op.id].g;
        el && el.classList.add('hov');
        text = hoverTextOp(op);
      }
    } else if (info.geomLine) {
      // شمارهٔ سطر در هاور روی خودِ مسیر (الزام نقشه↔کد)
      text = `سطر <span class="num">${info.geomLine}</span> — کلیک: هایلایت متقابل در نمای کد`;
    }
    if (text) showHud(info.client, text);
    CNC.emit('hover', { partId: info.partId || null, opId: info.opId || null });
  });
  function clearHover() {
    document.querySelectorAll('.hov').forEach((g) => g.classList.remove('hov'));
    if (hoverEl) { hoverEl.remove(); hoverEl = null; }
  }
  function findPart(id) { return ((state.detection && state.detection.parts) || []).find((p) => p.id === id); }
  function findOp(id) { return ((state.detection && state.detection.operations) || []).find((p) => p.id === id); }

  function hoverTextPart(part) {
    const b = part.bbox;
    const dims = `${CNC.f1(b.x_max - b.x_min)}×${CNC.f1(b.y_max - b.y_min)} mm`;
    const depth = (part.depths || []).filter((v) => v != null)
      .map((v) => CNC.f1(v)).join('، ') || '—';
    const caps = [];
    if (part.capabilities) {
      if (!part.capabilities.can_stretch_x && !part.capabilities.can_stretch_y) caps.push('کشش: ندارد');
      if (part.locked) caps.push('قفل (هم‌پوشانی)');
    }
    if (state.fixedParts.has(part.id)) caps.push('ثابت علامت خورده');
    return [
      `<b>${CNC.partName(part)}</b> — ${CNC.esc(CNC.SHAPE_FA[part.shape_kind] || part.shape_kind)}`,
      `ابعاد <span class="num">${dims}</span> · عمق <span class="num">${depth}</span> mm`
        + (part.tool != null ? ` · ابزار T<span class="num">${part.tool}</span>` : ''),
      caps.length ? `<span style="color:var(--warn)">${CNC.esc(caps.join(' · '))}</span>` : '',
    ].filter(Boolean).join('<br>');
  }
  function hoverTextOp(op) {
    const b = op.bbox;
    const dims = `${CNC.f1(b.x_max - b.x_min)}×${CNC.f1(b.y_max - b.y_min)} mm`;
    const owner = op.assigned_part ? CNC.partName(findPart(op.assigned_part) || {}) : 'بدون قطعهٔ مالک';
    return [
      `<b>${CNC.esc(CNC.KIND_FA[op.kind] || op.kind)}</b> <span class="num">${op.id}</span>`,
      `ابعاد <span class="num">${dims}</span>`
        + (op.tool_dia_used ? ` · ابزار <span class="num">${CNC.f1(op.tool_dia_used)}</span>mm` : '')
        + ` · متعلق به ${CNC.esc(owner)}`,
      op.shallow ? '<span style="color:var(--warn)">کم‌عمق (حکاکی‌سان)</span>' : '',
    ].filter(Boolean).join('<br>');
  }

  function showHud(client, html) {
    if (!hoverEl) {
      hoverEl = document.createElement('div');
      hoverEl.className = 'maphud';
      document.body.appendChild(hoverEl);
    }
    hoverEl.innerHTML = html;
    const pad = 14;
    let x = client.x + pad, y = client.y + pad;
    const rect = hoverEl.getBoundingClientRect();
    if (x + rect.width > window.innerWidth - 8) x = client.x - rect.width - pad;
    if (y + rect.height > window.innerHeight - 8) y = client.y - rect.height - pad;
    hoverEl.style.left = x + 'px';
    hoverEl.style.top = y + 'px';
  }
  CNC.hideHud = () => clearHover();

  // ------------------------------------------------_پیمایش Tab بین قطعات
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    // فقط وقتی فوکوس روی خودِ نقشه/بدنه است؛ روی دکمه‌ها/فیلدها/عناصر
    // فوکوس‌پذیر، Tab باید ناوبری عادی کیبورد بماند (bugfix: preventDefault
    // برای هر Tab ناوبری کیبورد کل صفحه را قفل می‌کرد).
    const target = e.target;
    if (['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'A'].includes(target.tagName)) return;
    if (target.isContentEditable) return;
    if (typeof target.closest === 'function'
        && target.closest('button, a, input, select, textarea, [contenteditable], [tabindex]')) return;
    const parts = (state.detection && state.detection.parts) || [];
    if (!parts.length) return;
    e.preventDefault();
    const ordered = parts.slice().sort((a, b) => a.index - b.index);
    const cur = sel().find((s) => s.type === 'part');
    const idx = cur ? ordered.findIndex((p) => p.id === cur.id) : -1;
    const next = ordered[(idx + 1 + (e.shiftKey ? ordered.length - 1 : 0)) % ordered.length];
    CNC.selection.set([{ type: 'part', id: next.id }]);
    CNC.emit('focus-part', { partId: next.id });   // بازرس/viewport می‌تواند زوم کند
  });
})();
