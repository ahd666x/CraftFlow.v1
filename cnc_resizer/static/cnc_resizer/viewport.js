/* ============================================================
   viewport.js — بوم تمام‌صفحه SVG، زوم/پن/پینچ، گرید و رندر هندسه
   هندسه مستقیم از IR موتور خوانده می‌شود (program_ir / program)؛
   هیچ محاسبهٔ هندسی جدیدی اختراع نمی‌شود — فقط نمایش.
   مختصات CNC (y رو به بالا) با وارونگی y به SVG می‌رود.
   ============================================================ */
(() => {
  'use strict';
  const CNC = window.CNC;
  const NS = 'http://www.w3.org/2000/svg';
  const state = CNC.state;

  const PART_COLORS = ['var(--p1)', 'var(--p2)', 'var(--p3)', 'var(--p4)', 'var(--p5)',
                       'var(--p6)', 'var(--p7)', 'var(--p8)', 'var(--p9)', 'var(--p10)'];
  CNC.partColor = (part) => PART_COLORS[((part.index || 1) - 1) % PART_COLORS.length];
  // فاز ۳.۲: همگام با پالت colorblind-safe در map.css (--p1..--p10)
  const DEPTH_COLORS = ['#1d6ce0', '#157a45', '#6d28d9', '#c2410c', '#0e7490',
                        '#be185d', '#4d7c0f', '#92400e', '#1e3a8a', '#334155'];
  CNC.depthColor = (z) => {
    const levels = (CNC.model && CNC.model.depthLevels) || [];
    const idx = levels.findIndex((v) => Math.abs(v - z) < 1e-6);
    return DEPTH_COLORS[(idx < 0 ? levels.length : idx) % DEPTH_COLORS.length];
  };
  const KIND_FA = {
    pocket_or_hole: 'Pocket/Hole', engraving_loop: 'Engraving (shallow loop)',
    groove: 'Open groove', saw_line: 'Saw line', raster: 'Raster/Hatching',
    engraving: 'Engraving', orphan_loop: 'Orphan loop',
  };
  CNC.KIND_FA = KIND_FA;
  CNC.SHAPE_FA = {
    rectangle: 'مستطیل', rounded_rect: 'مستطیل گوشه‌گرد', circle: 'دایره',
    obround: 'بیضی‌گون', polygon: 'چندضلعی', other: 'شکل دیگر',
  };

  let svg = null, vb = null;
  let gGrid, gSheet, gGapHl, gGhost, gCollide, gParts, gOps, gDims, gResizeMarks, gStretch, gHandles, gMeas, gSel, gHud, gFix;
  const refs = {};          // part_id -> {g, selbox, label, hit}
  const opEls = {};         // op_id  -> {g, selbox, hit}
  CNC.viewEls = { refs, opEls };

  function el(name, attrs, parent) {
    const e = document.createElementNS(NS, name);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  CNC.svgEl = el;

  // ================================================================_هندسه
  // هر بلوک IR → فهرست سگمنت‌های برش (خط/قوس) + متادیتا (خط مبدأ، ابزار، عمق)
  function buildBlockModel(ir) {
    const model = { blocks: new Map(), depthLevels: new Set(), tools: new Set() };
    const blocks = (ir && ir.blocks) || [];
    for (const block of blocks) {
      const segments = [];
      const lines = new Set();
      const depths = new Set();
      let tool = null;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const m of block.motions || []) {
        if (m.tool != null) tool = m.tool;
        if (m.motion === 'rapid' || !m.end || m.end.x == null || m.end.y == null) continue;
        if (m.start && m.start.x != null && m.start.y != null
            && (m.motion === 'linear' || m.motion === 'arc_cw' || m.motion === 'arc_ccw')) {
          const x1 = m.start.x, y1 = m.start.y, x2 = m.end.x, y2 = m.end.y;
          if (m.motion === 'linear') {
            segments.push({ t: 'line', x1, y1, x2, y2, line: m.source_line });
          } else if (m.arc && m.arc.valid && m.arc.radius) {
            // جهت قوس پس از وارونگی y (آینهٔ عمودی): پارسر sweep_radians را با
            // قرارداد ریاضی (y رو به بالا، CCW مثبت) می‌دهد — G2 منفی، G3 مثبت.
            // وارونگی y جهت دیداری را عوض می‌کند، پس G2 ساعت‌گردِ CNC روی
            // صفحه هم ساعت‌گرد دیده می‌شود → sweep SVG = 1؛ G3 → 0.
            // (قبلاً برعکس بود و هر قوس از سمت اشتباه وتر رسم می‌شد.)
            segments.push({
              t: 'arc', x1, y1, x2, y2, r: m.arc.radius,
              sweep: m.motion === 'arc_cw' ? 1 : 0,
              large: Math.abs(m.arc.sweep_radians || 0) > Math.PI,
              line: m.source_line,
            });
          } else {
            segments.push({ t: 'line', x1, y1, x2, y2, line: m.source_line });
          }
          if (m.end.z != null) depths.add(Math.round(m.end.z * 1000) / 1000);
          if (m.source_line != null) lines.add(m.source_line);
          for (const [px, py] of [[x1, y1], [x2, y2]]) {
            minX = Math.min(minX, px); maxX = Math.max(maxX, px);
            minY = Math.min(minY, py); maxY = Math.max(maxY, py);
          }
          const arc = m.arc;
          if (arc && arc.center && arc.center.x != null) {
            // نمونه‌های قوس برای کادر محیطی دقیق‌تر
            const cx = arc.center.x, cy = arc.center.y, r = arc.radius;
            for (let k = 0; k <= 8; k++) {
              const a0 = Math.atan2(y1 - cy, x1 - cx);
              let a1 = a0 + (arc.sweep_radians || 0);
              const a = a0 + (a1 - a0) * (k / 8);
              minX = Math.min(minX, cx + r * Math.cos(a)); maxX = Math.max(maxX, cx + r * Math.cos(a));
              minY = Math.min(minY, cy + r * Math.sin(a)); maxY = Math.max(maxY, cy + r * Math.sin(a));
            }
          }
        }
      }
      if (segments.length) {
        model.blocks.set(block.id, {
          id: block.id, segments, lines,
          depths: [...depths], tool,
          bbox: (minX === Infinity) ? null : { x_min: minX, y_min: minY, x_max: maxX, y_max: maxY },
          started: block.started_by_line, ended: block.ended_at_line,
        });
        depths.forEach((d) => model.depthLevels.add(d));
        if (tool != null) model.tools.add(tool);
      }
    }
    model.depthLevels = [...model.depthLevels].sort((a, b) => b - a); // عمق‌های عمیق‌تر اول
    model.tools = [...model.tools].sort((a, b) => a - b);
    return model;
  }
  CNC.buildBlockModel = buildBlockModel;

  function blockRefMap(detection) {
    // block_id → {type, refId} : نقشهٔ بلوک به قطعه/فرزند/عملیات
    const map = new Map();
    const parts = (detection && detection.parts) || [];
    for (const part of parts) {
      (part.contour_block_ids || []).forEach((id) => map.set(id, { type: 'part', refId: part.id }));
      (part.children || []).forEach((child) => {
        (child.block_ids || []).forEach((id) => map.set(id, { type: 'child', refId: child.id, parent: part.id }));
      });
    }
    ((detection && detection.operations) || []).forEach((op) => {
      (op.block_ids || []).forEach((id) => map.set(id, { type: 'op', refId: op.id }));
    });
    return map;
  }

  function pathD(segments) {
    // یک path برای هر سگمنت جدا نمی‌سازیم؛ زنجیرهٔ متصل را یکی می‌کنیم
    let d = '';
    let cx = null, cy = null;
    for (const s of segments) {
      if (s.t === 'line') {
        if (cx !== s.x1 || cy !== s.y1 || d === '') d += `M${s.x1} ${-s.y1}`;
        d += `L${s.x2} ${-s.y2}`;
        cx = s.x2; cy = s.y2;
      } else {
        d += `M${s.x1} ${-s.y1}A${s.r} ${s.r} 0 ${s.large ? 1 : 0} ${s.sweep} ${s.x2} ${-s.y2}`;
        cx = s.x2; cy = s.y2;
      }
    }
    return d;
  }
  CNC.pathD = pathD;

  function segPathD(s) {
    return s.t === 'line'
      ? `M${s.x1} ${-s.y1}L${s.x2} ${-s.y2}`
      : `M${s.x1} ${-s.y1}A${s.r} ${s.r} 0 ${s.large ? 1 : 0} ${s.sweep} ${s.x2} ${-s.y2}`;
  }
  CNC.segPathD = segPathD;

  // ================================================================_viewBox
  function applyVB() {
    if (!vb) return;
    svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    updateScalebar();
  }
  function pxPerMM() {
    const m = svg.getScreenCTM();
    return (m && m.a) || 0.2;
  }
  function sceneBBox() {
    // کادر کل صحنه از مدل بلوک‌ها + شیت
    const model = CNC.model;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    if (model) {
      for (const b of model.blocks.values()) {
        if (!b.bbox) continue;
        minX = Math.min(minX, b.bbox.x_min); maxX = Math.max(maxX, b.bbox.x_max);
        minY = Math.min(minY, b.bbox.y_min); maxY = Math.max(maxY, b.bbox.y_max);
      }
    }
    if (!isFinite(minX)) return { x0: -50, y0: -50, x1: 150, y1: 150 };
    return { x0: minX, y0: minY, x1: maxX, y1: maxY };
  }
  function computeFit() {
    const b = sceneBBox();
    const span = Math.max(b.x1 - b.x0, b.y1 - b.y0) || 1;
    const mx = Math.max(60, span * 0.12), my = Math.max(48, span * 0.1);
    vb = { x: b.x0 - mx, y: -(b.y1 + my), w: (b.x1 - b.x0) + mx * 2, h: (b.y1 - b.y0) + my * 2 };
    applyVB();
  }
  function zoomAt(f, cx, cy) {
    if (!vb) return;
    const p = cx == null ? null : toWorld(cx, cy);
    const nw = Math.min(200000, Math.max(5, vb.w * f));
    const k = nw / vb.w;
    if (p) {
      vb = { x: p.x - (p.x - vb.x) * k, y: p.y - (p.y - vb.y) * k, w: nw, h: vb.h * k };
    } else {
      vb = { x: vb.x + vb.w * (1 - k) / 2, y: vb.y + vb.h * (1 - k) / 2, w: nw, h: vb.h * k };
    }
    applyVB();
  }
  function toWorld(cx, cy) {
    const m = svg.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const pt = new DOMPoint(cx, cy).matrixTransform(m.inverse());
    return { x: pt.x, y: -pt.y };   // برگشت به مختصات CNC
  }
  CNC.viewport = {
    get svg() { return svg; },
    get vb() { return vb; },
    setVB(next) { vb = next; applyVB(); },
    fit: computeFit,
    zoomAt,
    toWorld,
    pxPerMM,
    applyVB,
    render,
  };

  // ================================================================_گرید
  function drawGrid() {
    gGrid.replaceChildren();
    if (!state.layers.showGrid || !vb) return;
    const px = pxPerMM();
    const target = 60 / px;
    const steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000];
    const major = steps.find((s) => s >= target) || 5000;
    const minor = major / 5;
    const showMinor = minor * px >= 7;
    const x0 = Math.floor(vb.x / minor) * minor, x1 = vb.x + vb.w;
    const y0 = Math.floor(vb.y / minor) * minor, y1 = vb.y + vb.h;
    if (showMinor) {
      for (let x = x0; x <= x1; x += minor) {
        const isMaj = Math.abs(x % major) < minor / 2 || Math.abs((x % major) - major) < minor / 2;
        el('line', { x1: x, y1: vb.y, x2: x, y2: y1, stroke: isMaj ? 'var(--grid-major)' : 'var(--grid-minor)', 'stroke-width': 1 }, gGrid);
      }
      for (let y = y0; y <= y1; y += minor) {
        const isMaj = Math.abs(y % major) < minor / 2 || Math.abs((y % major) - major) < minor / 2;
        el('line', { x1: vb.x, y1: y, x2: x1, y2: y, stroke: isMaj ? 'var(--grid-major)' : 'var(--grid-minor)', 'stroke-width': 1 }, gGrid);
      }
    } else {
      for (let x = Math.ceil(vb.x / major) * major; x <= x1; x += major) {
        el('line', { x1: x, y1: vb.y, x2: x, y2: y1, stroke: 'var(--grid-major)', 'stroke-width': 1 }, gGrid);
      }
      for (let y = Math.ceil(vb.y / major) * major; y <= y1; y += major) {
        el('line', { x1: vb.x, y1: y, x2: x1, y2: y, stroke: 'var(--grid-major)', 'stroke-width': 1 }, gGrid);
      }
    }
    // محورها + مبدأ
    el('line', { x1: vb.x, y1: 0, x2: x1, y2: 0, stroke: 'var(--feat-outer)', 'stroke-width': 1.6, opacity: .8 }, gGrid);
    el('line', { x1: 0, y1: vb.y, x2: 0, y2: y1, stroke: 'var(--feat-outer)', 'stroke-width': 1.6, opacity: .8 }, gGrid);
    el('circle', { cx: 0, cy: 0, r: 4, fill: 'none', stroke: 'var(--feat-outer)', 'stroke-width': 1.4 }, gGrid);
    const fs = 11 / px;
    for (let x = Math.ceil(vb.x / major) * major; x <= x1; x += major) {
      if (Math.abs(x) < 1e-9) continue;
      const t = el('text', { x, y: y1 - 4 / px, 'font-size': fs, fill: 'var(--muted)', 'text-anchor': 'middle', direction: 'ltr' }, gGrid);
      t.textContent = String(Math.round(x));
    }
    for (let y = Math.ceil(vb.y / major) * major; y <= y1; y += major) {
      if (Math.abs(y) < 1e-9) continue;
      const t = el('text', { x: vb.x + 4 / px, y: -y, 'font-size': fs, fill: 'var(--muted)', 'dominant-baseline': 'hanging', direction: 'ltr' }, gGrid);
      t.textContent = String(Math.round(-y));
    }
  }

  function updateScalebar() {
    const bar = document.getElementById('scalebarBar');
    const txt = document.getElementById('scalebarTxt');
    if (!bar) return;
    const px = pxPerMM();
    const nice = [10, 20, 50, 100, 200, 500, 1000, 2000].find((v) => v * px >= 70) || 5000;
    bar.style.width = (nice * px) + 'px';
    txt.textContent = nice + ' mm';
  }

  // ================================================================_رندر صحنه
  function clearGroups() {
    [gSheet, gGapHl, gGhost, gCollide, gParts, gOps, gDims, gResizeMarks, gStretch, gHandles, gMeas, gSel, gHud, gFix]
      .forEach((g) => g && g.replaceChildren());
    for (const k in refs) delete refs[k];
    for (const k in opEls) delete opEls[k];
  }

  function blockPathsInto(parent, block, cls, color, dash) {
    // هر سگمنت یک path با data-line برای همگام‌سازی کد↔نقشه
    for (const s of block.segments) {
      const p = el('path', {
        d: segPathD(s), class: `blk ${cls}`, stroke: color, 'data-line': s.line,
        'data-block': block.id, fill: 'none',
      }, parent);
      if (dash) p.setAttribute('stroke-dasharray', dash);
    }
  }

  function render() {
    if (!svg) return;
    clearGroups();
    const model = CNC.model;
    const detection = state.detection;
    const hasFile = !!(model && model.blocks.size);
    document.getElementById('emptyState').hidden = hasFile;
    document.getElementById('coords').hidden = !hasFile;
    document.getElementById('scalebar').hidden = !hasFile;
    document.getElementById('zoombtns').hidden = !hasFile;
    if (!hasFile) { drawGrid(); return; }
    if (!vb || state.needFit) { computeFit(); state.needFit = false; }

    // --- شیت ---
    const sheet = (state.preview && state.preview.resized && state.preview.resized.sheet_bbox_finished)
      || (state.fileMeta && state.fileMeta.sheet_bbox_finished);
    if (sheet) {
      // الزامی: گوشهٔ ورق همیشه مبدأ (0,0) — sheet_bbox_finished حاشیهٔ ابزار
      // را به bbox اضافه می‌کند و آفست x_min/y_min می‌دهد؛ مستطیل ورق نباید
      // با آن جابه‌جا شود چون هندسهٔ قطعات و محورهای گرید مرجعِ صفر هستند.
      const w = sheet.width != null ? sheet.width : (sheet.x_max - sheet.x_min);
      const h = sheet.height != null ? sheet.height : (sheet.y_max - sheet.y_min);
      el('rect', { x: 0, y: -h, width: w, height: h,
                   fill: 'var(--surface)', stroke: 'var(--line)', 'stroke-width': 1 }, gSheet);
    }

    // --- شبح «قبل» (از IR اصلی) ---
    if (state.preview && state.layers.showGhost && state.sourceIR) {
      const ghostModel = state._ghostModel || (state._ghostModel = buildBlockModel(state.sourceIR));
      const g = el('g', { class: 'ghost-g' }, gGhost);
      for (const b of ghostModel.blocks.values()) blockPathsInto(g, b, '', 'var(--ghost)');
    }

    // --- نقشهٔ مرجع بلوک‌ها (شناسه‌ها بین قبل/بعد پایدارند — وصلهٔ 1:1) ---
    const refMap = (state._refMap = blockRefMap(detection));
    // کپی‌های آرایه/گرید (feature-level): بلوک‌های تازهٔ بعدی با هندسهٔ
    // مشابه مبدأ → همان گروه/رنگ مبدأ
    if (state._copyMap) {
      for (const [newId, srcId] of state._copyMap) {
        if (!refMap.has(newId) && refMap.has(srcId)) refMap.set(newId, refMap.get(srcId));
      }
    }
    const claimed = new Set(refMap.keys());
    // نگاشت قطعهٔ قبل → قطعهٔ بعد بر اساس عضویت بلوک کانتور (bugfix):
    // digest شناسه به bbox وابسته است و پس از جابه‌جایی عوض می‌شود؛ ترتیب
    // sort هم (y_min, x_min) است و یک جابه‌جایی می‌تواند آن را برعکس کند —
    // جفت‌کردن index-محور بعد از ویرایش جفت‌های اشتباه می‌ساخت. بلوک‌ها بین
    // قبل/بعد پایدارند (وصلهٔ 1:1)، پس عضویت‌شان هویت پایدار قطعه است.
    const afterPairs = new Map();
    const afterParts = (state.preview && state.preview.resized && state.preview.resized.parts) || [];
    const afterByBlocks = new Map();
    afterParts.forEach((rp) => afterByBlocks.set((rp.contour_block_ids || []).join('|'), rp));
    ((detection && detection.parts) || []).forEach((p) => {
      const hit = afterByBlocks.get((p.contour_block_ids || []).join('|'));
      afterPairs.set(p.id, hit || p);
    });
    state._afterPairs = afterPairs;

    // --- قطعات ---
    const parts = (detection && detection.parts) || [];
    for (const part of parts) {
      const color = CNC.partColor(part);
      const g = el('g', { class: 'part-g', 'data-part': part.id, 'data-name': partName(part) }, gParts);
      // hit اول (زیر مسیرها): کلیک روی فضای خالی قطعه هم انتخاب می‌کند،
      // ولی کلیک روی سطرِ مسیر به خود path می‌رسد (نقشه↔کد)
      const blocks0 = [];
      (part.contour_block_ids || []).forEach((id) => { const b = model.blocks.get(id); if (b) blocks0.push(b); });
      (part.children || []).forEach((child) => {
        (child.block_ids || []).forEach((id) => { const b = model.blocks.get(id); if (b) blocks0.push(b); });
      });
      const bb0 = unionBBox(blocks0);
      if (bb0) {
        el('rect', {
          class: 'hit', x: bb0.x_min - 2, y: -bb0.y_max - 2,
          width: bb0.x_max - bb0.x_min + 4, height: bb0.y_max - bb0.y_min + 4,
          'pointer-events': 'all',
        }, g).dataset.part = part.id;
      }
      const blocks = [];
      (part.contour_block_ids || []).forEach((id) => {
        const b = model.blocks.get(id);
        if (b) { blockPathsInto(g, b, 'contour', color); blocks.push(b); claimed.add(id); }
      });
      (part.children || []).forEach((child) => {
        (child.block_ids || []).forEach((id) => {
          const b = model.blocks.get(id);
          if (b) { blockPathsInto(g, b, 'child', color); blocks.push(b); claimed.add(id); }
        });
      });
      // کادر انتخاب + برچسب
      const bb = unionBBox(blocks);
      if (bb) {
        el('rect', {
          class: 'selbox', x: bb.x_min - 3, y: -bb.y_max - 3,
          width: bb.x_max - bb.x_min + 6, height: bb.y_max - bb.y_min + 6, rx: 2,
        }, g);
        const px = pxPerMM();
        const label = el('text', {
          x: bb.x_min + 2 / px, y: -bb.y_min - 4 / px, 'font-size': 12 / px, fill: color,
          'font-weight': 800, direction: 'ltr',
          style: 'paint-order:stroke; stroke:var(--canvas); stroke-width:3px;',
        }, g);
        label.textContent = partName(part);
        refs[part.id] = { g, selbox: g.querySelector('.selbox'), label, hit: g.querySelector('.hit'), bb, color };
      }
      applyPartState(part.id);
    }

    // --- عملیات باز ---
    const ops = (detection && detection.operations) || [];
    const KIND_COLOR = {
      groove: 'var(--feat-groove)', saw_line: 'var(--feat-groove)',
      raster: 'var(--feat-raster)', engraving: 'var(--feat-hole)',
      engraving_loop: 'var(--feat-hole)', orphan_loop: 'var(--feat-unknown)',
    };
    if (state.layers.showOps) {
      for (const op of ops) {
        const color = KIND_COLOR[op.kind] || 'var(--feat-unknown)';
        const g = el('g', { class: 'op-g', 'data-op': op.id }, gOps);
        const blocks = [];
        (op.block_ids || []).forEach((id) => {
          const b = model.blocks.get(id);
          if (b) { blockPathsInto(g, b, 'op', color); blocks.push(b); claimed.add(id); }
        });
        const bb = unionBBox(blocks);
        if (bb) {
          el('rect', {
            class: 'selbox', x: bb.x_min - 3, y: -bb.y_max - 3,
            width: bb.x_max - bb.x_min + 6, height: bb.y_max - bb.y_min + 6, rx: 2,
          }, g);
          const hit = el('rect', {
            class: 'hit op-hit', x: bb.x_min - 2, y: -bb.y_max - 2,
            width: bb.x_max - bb.x_min + 4, height: bb.y_max - bb.y_min + 4, fill: 'transparent',
          }, g);
          hit.dataset.op = op.id;
          opEls[op.id] = { g, selbox: g.querySelector('.selbox'), hit, bb, color };
        }
      }
    }

    // --- بلوک‌های بی‌مالک (هیچ قطعه/عملیاتی) ---
    for (const b of model.blocks.values()) {
      if (claimed.has(b.id)) continue;
      blockPathsInto(gOps, b, 'op orphan', 'var(--feat-unknown)', '2 3');
    }

    // --- برخوردها (قرمز) — flagged از شناسه‌های «قبل» استفاده می‌کند ---
    const flagged = (state.preview && state.preview.collision_check && state.preview.collision_check.flagged) || [];
    for (const f of flagged) {
      for (const pid of f.parts || []) {
        const after = afterPairs.get(pid);
        if (after && after.bbox_finished) {
          const b = after.bbox_finished;
          el('rect', { class: 'collision-box', x: b.x_min, y: -b.y_max, width: b.width, height: b.height }, gCollide);
        }
      }
    }
    if (flagged.length) {
      for (const pid in refs) refs[pid].g.classList.add('collision');
    }

    drawResizeIncrements();
    drawFixtures();
    drawMeasures();
    drawGrid();
    applyLayerVisibility();
    CNC.emit('rendered', {});
  }

  function unionBBox(blocks) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const b of blocks) {
      if (!b.bbox) continue;
      minX = Math.min(minX, b.bbox.x_min); maxX = Math.max(maxX, b.bbox.x_max);
      minY = Math.min(minY, b.bbox.y_min); maxY = Math.max(maxY, b.bbox.y_max);
    }
    return minX === Infinity ? null : { x_min: minX, y_min: minY, x_max: maxX, y_max: maxY };
  }
  CNC.unionBBox = unionBBox;

  function partName(part) { return 'P' + (part.index || 1); }
  CNC.partName = partName;

  // وضعیت‌های per-part: انتخاب/هاور/خروجی‌حذف‌شده/ثابت
  function applyPartState(partId) {
    const ref = refs[partId];
    if (!ref) return;
    ref.g.classList.toggle('sel', state.selection.some((s) => s.type === 'part' && s.id === partId));
    if (state.outputsExcluded.has(partId)) {
      ref.g.style.opacity = '.35';
      ref.g.setAttribute('filter', 'url(#hatch)');
    } else {
      ref.g.style.opacity = '';
      ref.g.removeAttribute('filter');
    }
  }
  CNC.applyPartStates = () => { for (const pid in refs) applyPartState(pid); };

  // ================================================================_لایه‌ها
  function applyLayerVisibility() {
    const L = state.layers;
    const model = CNC.model;
    if (!model) return;
    const blockVisible = (blockId) => {
      const b = model.blocks.get(blockId);
      if (!b) return true;
      if (L.tool.size && b.tool != null && L.tool.get(String(b.tool)) === false) return false;
      // بازبینی دور ۳: بلاک چندعمقی اگر «هر یک» از عمق‌هایش در لایهٔ روشن باشد
      // دیده می‌شود — حلقهٔ قبلی روی اولین عمق صریح کوتاه می‌شد و خاموش‌کردن
      // عمق دوم، بلاکی را که هنوز عمق روشن داشت قایم می‌کرد (یا برعکس).
      return !b.depths.length || b.depths.some((d) => L.depth.get(String(d)) !== false);
    };
    svg.querySelectorAll('path[data-block]').forEach((p) => {
      p.style.display = blockVisible(p.dataset.block) ? '' : 'none';
    });
    // ابعاد و شبح
    if (gGhost) gGhost.style.display = (state.preview && L.showGhost) ? '' : 'none';
    if (gDims) gDims.style.display = L.showDims ? '' : 'none';
    if (gOps) gOps.style.display = L.showOps ? '' : 'none';
    if (gSheet) gSheet.style.display = '';
  }
  CNC.applyLayerVisibility = applyLayerVisibility;

  // ================================================================_فیکسچرها
  let gFixOn = true;
  function fixturePoint(p) {
    const setup = CNC.setup;
    if (!setup) return null;
    return { x: p.x - (setup.origin?.x || 0), y: p.y - (setup.origin?.y || 0) };
  }
  function fixtureForbidden(p, bb) {
    const setup = CNC.setup;
    if (!bb || !setup) return null;
    const q = fixturePoint(p);
    const reach = (p.radius || 0) + (setup.safety_margin || 0);
    return (bb.x0 - reach <= q.x && q.x <= bb.x1 + reach
        && bb.y0 - reach <= q.y && q.y <= bb.y1 + reach);
  }
  function drawFixtures() {
    gFix.replaceChildren();
    if (!gFixOn || !CNC.setup || !CNC.setup.points || !CNC.setup.points.length) return;
    const bb0 = sceneBBox();
    const bb = { x0: bb0.x0, x1: bb0.x1, y0: bb0.y0, y1: bb0.y1 };
    const px = pxPerMM();
    for (const p of CNC.setup.points) {
      const q = fixturePoint(p);
      const forbidden = fixtureForbidden(p, bb);
      const color = forbidden === true ? 'var(--bad)' : forbidden === false ? 'var(--good)' : 'var(--ghost)';
      const cy = -q.y;
      el('circle', { cx: q.x, cy, r: p.radius || 10, fill: color, 'fill-opacity': 0.07,
                     stroke: color, 'stroke-opacity': .5, 'stroke-width': 1, 'stroke-dasharray': '4 3' }, gFix);
      if (forbidden === true) {
        const s = Math.max(6 / px, (p.radius || 10) * .35);
        el('path', { d: `M${q.x - s} ${cy - s}L${q.x + s} ${cy + s}M${q.x - s} ${cy + s}L${q.x + s} ${cy - s}`,
                     stroke: 'var(--bad)', fill: 'none', 'stroke-width': 2.4 }, gFix);
      } else {
        el('circle', { cx: q.x, cy, r: Math.max(2.5 / px, 2), fill: color, stroke: 'none' }, gFix);
      }
      const fs = 11 / px;
      const t = el('text', { x: q.x, y: cy - (p.radius || 10) - 3 / px, 'font-size': fs,
                             fill: color, 'text-anchor': 'middle', 'font-weight': 700,
                             style: 'paint-order:stroke; stroke:var(--canvas); stroke-width:3px;' }, gFix);
      t.textContent = p.label;
    }
  }
  CNC.toggleFixtures = (on) => { gFixOn = on; drawFixtures(); };

  // ================================================================_اندازه‌گیری
  function drawMeasures() {
    gMeas.replaceChildren();
    const all = state.measures.slice();
    const live = state._measureLive;
    if (live && live.b) all.push({ ax: live.a.x, ay: live.a.y, bx: live.b.x, by: live.b.y });
    const px = pxPerMM();
    for (const m of all) {
      const color = (live && live.b && m.bx === live.b.x && m.by === live.b.y) ? 'var(--feat-groove)' : 'var(--feat-outer)';
      const ax = m.ax, ay = -m.ay, bx = m.bx, by = -m.by;
      const dx = m.bx - m.ax, dy = m.by - m.ay;
      const L = Math.hypot(dx, dy);
      const s = 7 / px;
      const ux = dx / (L || 1), uy = dy / (L || 1);
      const wx = -uy, wy = ux;
      el('line', { x1: ax - wx * s, y1: ay - wy * s, x2: ax + wx * s, y2: ay + wy * s, stroke: color, 'stroke-width': 1.2 }, gMeas);
      el('line', { x1: bx - wx * s, y1: by - wy * s, x2: bx + wx * s, y2: by + wy * s, stroke: color, 'stroke-width': 1.2 }, gMeas);
      el('line', { x1: ax, y1: ay, x2: bx, y2: by, stroke: color, 'stroke-width': 1.4 }, gMeas);
      const t = el('text', { x: (ax + bx) / 2 + wx * s * 1.2, y: (ay + by) / 2 + wy * s * 1.2,
                             'font-size': 12 / px, fill: color, 'text-anchor': 'middle',
                             'dominant-baseline': 'middle', direction: 'ltr' }, gMeas);
      t.textContent = `${CNC.f2(L)} mm  ΔX ${CNC.f1(dx)}  ΔY ${CNC.f1(dy)}`;
    }
  }
  CNC.drawMeasures = drawMeasures;

  // نمایش سهم واقعی هر فاصله روی نقشهٔ فایل مشتق یا نتیجهٔ محاسبه‌شده.
  function drawResizeIncrements() {
    if (!gResizeMarks) return;
    gResizeMarks.replaceChildren();
    const gaps = state.resizeGaps || [];
    const bb = sceneBBox();
    const px = pxPerMM();
    gaps.forEach((gap, index) => {
      const amount = Number(gap.increment) || 0;
      if (Math.abs(amount) < 0.05) return;
      const y0 = Number(gap.display_y0 ?? gap.y0);
      const y1 = Number(gap.display_y1 ?? gap.y1);
      if (!Number.isFinite(y0) || !Number.isFinite(y1)) return;
      const x = bb.x0 - 8 / px;
      const y = -(y0 + y1) / 2;
      const label = `${amount > 0 ? '+' : ''}${CNC.f1(amount)} mm`;
      const t = el('text', {
        x, y, 'font-size': 14 / px, fill: 'var(--accent)', 'font-weight': 800,
        'text-anchor': 'end', 'dominant-baseline': 'middle', direction: 'ltr',
        style: 'paint-order:stroke; stroke:var(--canvas); stroke-width:4px; stroke-linejoin:round; pointer-events:none;',
      }, gResizeMarks);
      t.textContent = label;
      const lineY = y;
      el('line', { x1: bb.x0 - 5 / px, y1: lineY, x2: bb.x0 + 5 / px, y2: lineY,
                   stroke: 'var(--accent)', 'stroke-width': 1.5 / px, 'stroke-dasharray': `${3 / px} ${2 / px}`,
                   'pointer-events': 'none' }, gResizeMarks);
      t.setAttribute('aria-label', `فاصله ${index + 1}: افزایش ${label}`);
    });
  }

  // ================================================================_init
  CNC.viewport.init = function init(svgEl) {
    svg = svgEl;
    const defs = el('defs', {}, svg);
    const pattern = el('pattern', { id: 'hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
    el('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: 'var(--bad)', 'stroke-width': 2, 'stroke-opacity': .5 }, pattern);
    gGrid = el('g', { id: 'gGrid' }, svg);
    gSheet = el('g', { id: 'gSheet' }, svg);
    gGapHl = el('g', { id: 'gGapHl', 'pointer-events': 'none' }, svg);
    gGhost = el('g', { id: 'gGhost' }, svg);
    gCollide = el('g', { id: 'gCollide' }, svg);
    gParts = el('g', { id: 'gParts' }, svg);
    gOps = el('g', { id: 'gOps' }, svg);
    gDims = el('g', { id: 'gDims' }, svg);
    gResizeMarks = el('g', { id: 'gResizeMarks', 'pointer-events': 'none' }, svg);
    gStretch = el('g', { id: 'gStretch' }, svg);
    gHandles = el('g', { id: 'gHandles' }, svg);
    gMeas = el('g', { id: 'gMeas' }, svg);
    gSel = el('g', { id: 'gSel' }, svg);
    gHud = el('g', { id: 'gHud', 'pointer-events': 'none' }, svg);
    gFix = el('g', { id: 'gFix', 'pointer-events': 'none' }, svg);
    CNC.groups = { gGrid, gSheet, gGapHl, gGhost, gCollide, gParts, gOps, gDims, gResizeMarks, gStretch, gHandles, gMeas, gSel, gHud, gFix };
    bindGestures();
    drawGrid();
  };

  // هایلایت یک فاصله (بین گروه‌های سوراخ) از جدول پنل «طول» — زیر هندسه
  CNC.viewport.gapHighlight = function gapHighlight(y0, y1) {
    if (!gGapHl) return;
    gGapHl.replaceChildren();
    if (y0 == null || y1 == null || y1 <= y0) return;
    const sheet = (state.fileMeta && (state.fileMeta.sheet_bbox_finished || state.fileMeta.sheet_bbox)) || null;
    if (!sheet) return;
    el('rect', {
      x: sheet.x_min, y: -y1, width: sheet.width || (sheet.x_max - sheet.x_min), height: y1 - y0,
      class: 'gap-hl',
    }, gGapHl);
  };

  // ================================================================_ژست‌ها
  function targetInfo(target) {
    const partEl = target && target.closest ? target.closest('[data-part]') : null;
    const opEl = target && target.closest ? target.closest('[data-op]') : null;
    const dimEl = target && target.closest ? target.closest('[data-dim]') : null;
    const handleEl = target && target.closest ? target.closest('[data-handle]') : null;
    const barEl = target && target.closest ? target.closest('[data-bar]') : null;
    const lineEl = target && target.closest ? target.closest('path[data-line]') : null;
    return {
      partId: partEl ? partEl.dataset.part : null,
      opId: opEl ? opEl.dataset.op : null,
      dimId: dimEl ? dimEl.dataset.dim : null,
      handle: handleEl ? handleEl.dataset.handle : null,
      bar: barEl ? barEl.dataset.bar : null,
      geomLine: lineEl ? Number(lineEl.dataset.line) : null,
      onEmpty: !partEl && !opEl && !dimEl && !handleEl && !barEl,
    };
  }

  function bindGestures() {
    svg.addEventListener('wheel', (e) => {
      e.preventDefault();
      zoomAt(Math.exp(e.deltaY * 0.0015), e.clientX, e.clientY);
    }, { passive: false });

    const pointers = new Map();     // پشتیبانی پینچ دو-انگشتی
    let moved = false, downInfo = null, pinchDist = 0;
    // بازبینی UI/UX بند ۱۸: راست‌کلیک روی لمس وجود ندارد — نگه‌داشتن انگشت روی
    // قطعه (۵۵۰ms بدون جابه‌جایی) همان منوی عملیات (آرایه/آینه/چرخش/پین) را
    // باز می‌کند. اگر گِست درگ زودتر ادعا شود مشکلی نیست: انگشتِ بی‌حرکت هیچ
    // جابه‌جایی ثبت نمی‌کند و بعد از بازشدن منو، کلیک با moved=true می‌خوابد.
    let lpTimer = 0;
    const cancelLongPress = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = 0; } };
    svg.addEventListener('pointerdown', (e) => {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      try { svg.setPointerCapture(e.pointerId); } catch (_) { /* pointer نامعتبر */ }
      moved = false;
      const tgt = targetInfo(e.target);
      const w = toWorld(e.clientX, e.clientY);
      const info = {
        client: { x: e.clientX, y: e.clientY }, world: w, button: e.button,
        shift: e.shiftKey, ctrl: e.ctrlKey, alt: e.altKey, pointerType: e.pointerType,
        ...tgt,
      };
      // پن: دکمهٔ میانی/راست، Alt+کشیدن، یا تک‌انگشت لمسی روی فضای خالی
      const panMode = e.button === 1 || e.button === 2 || e.altKey
        || (e.pointerType !== 'mouse' && tgt.onEmpty && !CNC.state._gestureClaimed);
      const claimed = CNC.state._gestureClaimed;   // edit-tools (درگ قطعه/نوار کشش) زودتر گرفته
      if (pointers.size === 1) {
        // توجه: setPointerCapture همهٔ رویدادهای بعدی را به خود svg ریدایرکت
        // می‌کند؛ هدف اصلی کلیک باید همین‌جا ذخیره شود.
        downInfo = { ...info, tgt, panMode: panMode && !claimed, claimed: !!claimed };
        CNC.emit('map-down', { ...info, panMode: panMode && !claimed, claimed: !!claimed });
        cancelLongPress();
        if (e.pointerType !== 'mouse' && e.button === 0 && CNC.state.tool === 'select'
            && (tgt.partId || tgt.opId) && !tgt.dimId && !tgt.bar && !tgt.handle) {
          const lpInfo = { client: { x: e.clientX, y: e.clientY }, world: w, ...tgt };
          lpTimer = setTimeout(() => {
            lpTimer = 0;
            if (moved || pointers.size !== 1) return;
            try { if (navigator.vibrate) navigator.vibrate(15); } catch (_) { /* بدون لرزش */ }
            moved = true;              // بعد از برداشتن انگشت، کلیک محسوب نشود
            CNC.emit('map-context', lpInfo);
          }, 550);
        }
      } else if (pointers.size === 2) {
        cancelLongPress();
        const [a, b] = [...pointers.values()];
        pinchDist = Math.hypot(a.x - b.x, a.y - b.y);
        CNC.emit('map-gesture', { kind: 'pinch-start' });
      }
    });
    svg.addEventListener('pointermove', (e) => {
      const w = toWorld(e.clientX, e.clientY);
      const coordEl = document.getElementById('coordXY');
      if (coordEl) coordEl.textContent = (CNC.model && CNC.model.blocks.size)
        ? `X ${w.x.toFixed(1)}   Y ${w.y.toFixed(1)}` : '';
      if (!pointers.has(e.pointerId)) {
        CNC.emit('map-hover', {
          world: w, client: { x: e.clientX, y: e.clientY }, ...targetInfo(e.target),
        });
        return;
      }
      const prev = pointers.get(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (Math.abs(e.clientX - (downInfo?.client.x ?? e.clientX)) > 4
          || Math.abs(e.clientY - (downInfo?.client.y ?? e.clientY)) > 4) moved = true;
      if (moved) cancelLongPress();
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (pinchDist > 0 && Math.abs(d - pinchDist) > 1) {
          const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
          zoomAt(pinchDist / d, mid.x, mid.y);
          pinchDist = d;
        }
        return;
      }
      CNC.emit('map-move', {
        client: { x: e.clientX, y: e.clientY }, world: w,
        dx: e.clientX - prev.x, dy: e.clientY - prev.y,
        moved, down: downInfo ? { ...downInfo } : null,
        ...targetInfo(e.target),
      });
      if (downInfo && downInfo.panMode) {
        const m = svg.getScreenCTM();
        if (m && vb) {
          vb.x -= (e.clientX - prev.x) / m.a;
          vb.y -= (e.clientY - prev.y) / m.d;
          applyVB();
        }
        svg.classList.add('dragging');
      }
    });
    const finish = (e, kind) => {
      pointers.delete(e.pointerId);
      cancelLongPress();
      svg.classList.remove('dragging');
      if (pointers.size < 2) pinchDist = 0;
      if (pointers.size === 0 && downInfo) {
        const info = {
          client: { x: e.clientX, y: e.clientY }, world: toWorld(e.clientX, e.clientY),
          button: downInfo.button, moved,
          shift: e.shiftKey, ctrl: e.ctrlKey, alt: e.altKey,
          // هدفِ ذخیره‌شده در pointerdown (کپچر شدن، e.target را به svg تغییر می‌دهد)
          ...(downInfo.tgt || targetInfo(e.target)),
        };
        CNC.emit('map-up', info);
        if (!moved && downInfo.button === 0 && !downInfo.panMode && !downInfo.claimed
            && kind === 'up' && e.button === 0) {
          CNC.emit('map-click', info);
        }
        downInfo = null;
      }
      CNC.state._gestureClaimed = false;
    };
    svg.addEventListener('pointerup', (e) => finish(e, 'up'));
    svg.addEventListener('pointercancel', (e) => finish(e, 'cancel'));
    svg.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      // مثل کلیک: کپچرِ pointerdown هدف را به svg می‌برد — هدف ذخیره‌شده
      // در downInfo ملاک است.
      CNC.emit('map-context', {
        client: { x: e.clientX, y: e.clientY }, world: toWorld(e.clientX, e.clientY),
        ...((downInfo && downInfo.tgt) || targetInfo(e.target)),
      });
    });
    svg.addEventListener('dblclick', () => computeFit());
    window.addEventListener('resize', applyVB);
  }
})();
