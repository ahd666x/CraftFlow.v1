/* ============================================================
   edit-tools.js — ویرایش روی خودِ نقشه (پرامپت ۲، فاز «ب»)
   * خطوط اندازه (ابعاد قطعه + فاصلهٔ بین قطعه‌ها) → کلیک = ورودی عددی
     در همان‌جا؛ Enter اعمال می‌کند (جایگزین delta/target/جدول فاصله‌ها)
   * درگ قطعه برای جابه‌جایی با snap به لبه/مرکز بقیه + نمایش زندهٔ فاصله‌ها
   * نوارهای کشش: لبهٔ آزاد سبز قابل درگ، لنگر قفل؛ قوس/فرزند قفل خاکستری
   * دسته‌های لبه برای تغییر اندازه فقط با اجازهٔ capabilities
   * منوی راست‌کلیک: ثابت/کشیدنی، آرایه، آینه، چرخش ۹۰°، حذف از خروجی
   * پیش‌نمایش زندهٔ سمت کلاینت حین درگ؛ تأیید نهایی همیشه سمت سرور
   مدل ویرایش: هر قطعه حداکثر یک op «مطلق نسبت به فایل اصلی»
   (قرارداد سرور: هر بلوک در یک درخواست یک تبدیل).
   ============================================================ */
(() => {
  'use strict';
  const CNC = window.CNC;
  const state = CNC.state;
  const esc = CNC.esc;
  const el = CNC.svgEl;

  // ================================================================_پیش‌نمایش سرور
  // بازخورد اپراتور: «feature-block-0001» هیچ معنایی برای اپراتور ندارد؛
  // جفت‌های پرچم‌شدهٔ گتهٔ برخورد به نام قطعه/عملیات و اعداد فارسی ترجمه می‌شوند.
  function featureOwnerLabel(fid) {
    const blockId = String(fid || '').replace(/^feature-/, '');
    for (const p of (state.detection && state.detection.parts) || []) {
      if ((p.contour_block_ids || []).includes(blockId)) return CNC.partName(p);
      for (const c of p.children || []) {
        if ((c.block_ids || []).includes(blockId)) {
          return `${CNC.partName(p)} (${CNC.KIND_FA[c.kind] || c.kind})`;
        }
      }
    }
    for (const o of (state.detection && state.detection.operations) || []) {
      if ((o.block_ids || []).includes(blockId)) return CNC.KIND_FA[o.kind] || o.id;
    }
    return fid;
  }
  function describeCollision(res) {
    const flagged = (res && res.collision_check && res.collision_check.flagged) || [];
    if (!flagged.length) return null;
    const items = flagged.slice(0, 4).map((f) => {
      const a = featureOwnerLabel(f.features && f.features[0]);
      const b = featureOwnerLabel(f.features && f.features[1]);
      const dims = `<span class="num">${CNC.f1(f.overlap_x)}×${CNC.f1(f.overlap_y)} mm</span>`;
      if (f.severity === 'warning') {
        return `<b>${esc(a)}</b> و <b>${esc(b)}</b> فقط در کادر محیطی هم‌پوشانی دارند (${dims})`
          + (f.path_min_distance != null
              ? ` ولی مسیرهای برششان <span class="num">${CNC.f1(f.path_min_distance)} mm</span> فاصله دارد`
              : '');
      }
      return `<b>${esc(a)}</b> و <b>${esc(b)}</b> (${dims})`;
    }).join('؛ ');
    const more = flagged.length > 4 ? ` و ${flagged.length - 4} جفت دیگر` : '';
    return `${items}${more}`;
  }

  const requestPreview = CNC.debounce(async function requestPreview() {
    if (!state.text) return;
    const seq = ++state.previewSeq;   // race-guard: آخرین پاسخ می‌برد، کهنه‌ها دور ریخته می‌شوند
    const payload = CNC.opsPayload();
    if (payload.kind === 'none') {
      state.preview = null; state.currentIR = state.sourceIR;
      state.lastPreviewError = null; state._copyMap = null;
      CNC.model = CNC.buildBlockModel(state.currentIR);
      CNC.emit('rerender', {});
      CNC.emit('preview', null);
      CNC.refreshGate();
      return;
    }
    try {
      const res = await CNC.api.preview(state.text, state.filename, payload.edits);
      if (seq !== state.previewSeq) return; // پاسخ کهنه — ویرایش/فایل عوض شده
      state.preview = res;
      state.currentIR = res.program_ir || state.sourceIR;
      state.lastPreviewError = null;
      state._copyMap = buildCopyMap(res);
      CNC.model = CNC.buildBlockModel(state.currentIR);
      CNC.emit('rerender', {});
      CNC.emit('preview', res);
      CNC.refreshGate();
      const errs = (res.validation && res.validation.errors) || [];
      if (errs.length) {
        const human = describeCollision(res);
        CNC.alert('err', 'پیش‌نمایش با خطا: '
          + (human ? human + ' — ' : '')
          + errs.map(esc).join(' — ')
          + '<br>یکی از قطعه‌ها را جابه‌جا یا کوچک‌تر کنید؛ با واگرد (Ctrl+Z) هم آخرین تغییر برمی‌گردد.', 'preview');
      } else {
        // جفت‌های «فقط کادری» (مسیرها فاصله دارند) به هشدار downgrade شده‌اند —
        // اپراتور باید ببیند که پیش‌نمایش صادر شده ولی کدام جفت‌ها نزدیک‌اند.
        const soft = describeCollision(res);
        if (soft) CNC.alert('warn', 'توجه — ' + soft
          + '. مسیرها تداخل ندارند و پیش‌نمایش صادر شد؛ فاصلهٔ قطعات پس از برش (قطر ابزار) را خودتان چک کنید.', 'preview');
        else { CNC.clearAlert('preview'); CNC.clearAlert('preview-api'); }
      }
    } catch (err) {
      if (seq !== state.previewSeq) return;
      state.lastPreviewError = err.message;
      CNC.alert('err', 'ویرایش اعمال نشد: ' + esc(err.message)
        + ' — با واگرد (Ctrl+Z) آخرین تغییر را برگردانید.', 'preview-api');
      CNC.emit('gate', { ...state.gate });
    }
  }, 260);
  CNC.requestPreview = requestPreview;

  // bugfix: ویرایش قطعه‌ای بعد از آرایه/آینه/چرخش بی‌صدا نادیده گرفته می‌شد
  // (opsPayload بخش feature را می‌فرستد و partOp به سرور نمی‌رسید، ولی درگ/
  // کشش/دسته/Enter این محافظ را نداشتند و کاربر «ویرایش ثبت‌شده» می‌دید).
  // حالا همهٔ مسیرهای ویرایش قطعه‌ای از همین گارد می‌گذرند.
  function partEditBlocked(client) {
    if (!state.featureOps.length) return false;
    const msg = 'الان ویرایش‌های ویژگی‌محور (آرایه/آینه/چرخش) فعال است؛ ویرایش قطعه‌ای با آن در یک درخواست ترکیب نمی‌شود — '
      + 'اول ویرایش ویژگی‌محور را واگرد کنید یا از تاریخچه به قبل برگردید.';
    if (client) CNC.showTransient(client, esc(msg));
    else CNC.alert('warn', esc(msg));
    return true;
  }
  CNC.partEditBlocked = partEditBlocked;

  function commitOp(partId, op, label) {
    state.partOp.set(partId, op);
    CNC.emit('ops-changed', { partId, op, label });
    requestPreview();
  }
  CNC.commitOp = commitOp;
  function resetPartOp(partId) {
    state.partOp.delete(partId);
    CNC.emit('ops-changed', { partId, op: null, label: 'بازگردانی' });
    requestPreview();
  }
  CNC.resetPartOp = resetPartOp;

  // نگاشت کپی‌های آرایه به بلوک مبدأ: پاسخ feature-ops فقط «مبدأ + دلتاها»
  // را می‌دهد؛ با تطبیق کادر محیطیِ جابه‌جاشده، بلوک تازه پیدا می‌شود تا
  // کپی‌ها روی نقشه با رنگ/گروه قطعهٔ مبدأ رندر شوند.
  function buildCopyMap(res) {
    const copies = (res && res.block_copies) || [];
    if (!copies.length || !res.program_ir) return null;
    const model = CNC.buildBlockModel(res.program_ir);
    const map = new Map();
    const unknown = [...model.blocks.values()].filter((b) => {
      const det = state.detection;
      const known = new Set();
      (det.parts || []).forEach((p) => {
        (p.contour_block_ids || []).forEach((id) => known.add(id));
        (p.children || []).forEach((c) => (c.block_ids || []).forEach((id) => known.add(id)));
      });
      (det.operations || []).forEach((o) => (o.block_ids || []).forEach((id) => known.add(id)));
      return !known.has(b.id);
    });
    for (const copy of copies) {
      const src = model.blocks.get(copy.source_block_id);
      if (!src || !src.bbox) continue;
      for (const delta of copy.deltas || []) {
        const want = {
          x_min: src.bbox.x_min + (delta.dx || 0), x_max: src.bbox.x_max + (delta.dx || 0),
          y_min: src.bbox.y_min + (delta.dy || 0), y_max: src.bbox.y_max + (delta.dy || 0),
        };
        const hit = unknown.find((b) => b.bbox
          && Math.abs(b.bbox.x_min - want.x_min) < 0.02 && Math.abs(b.bbox.x_max - want.x_max) < 0.02
          && Math.abs(b.bbox.y_min - want.y_min) < 0.02 && Math.abs(b.bbox.y_max - want.y_max) < 0.02);
        if (hit) { map.set(hit.id, copy.source_block_id); unknown.splice(unknown.indexOf(hit), 1); }
      }
    }
    return map.size ? map : null;
  }

  // ================================================================_کمک‌های هندسی
  function findPart(id) { return ((state.detection && state.detection.parts) || []).find((p) => p.id === id); }
  function afterBox(partId) {
    const ref = CNC.viewEls.refs[partId];
    return ref ? ref.bb : null;
  }
  function beforeBox(partId) {
    const p = findPart(partId);
    return p ? p.bbox : null;
  }
  function appliedDelta(partId) {
    // جابه‌جایی اعمال‌شدهٔ فعلی نسبت به «قبل» (از هندسهٔ رندرشده)
    const a = afterBox(partId), b = beforeBox(partId);
    if (!a || !b) return { dx: 0, dy: 0 };
    return { dx: (a.x_min + a.x_max) / 2 - (b.x_min + b.x_max) / 2,
             dy: (a.y_min + a.y_max) / 2 - (b.y_min + b.y_max) / 2 };
  }
  CNC.appliedDelta = appliedDelta;

  function finishedBox(part) {
    return part.bbox_finished || part.bbox;
  }
  function stretchReason(part, axis) {
    if (part.locked) return 'قطعه قفل است (کانتورهای هم‌پوشان).';
    if (state.fixedParts.has(part.id)) return 'این قطعه «ثابت» علامت خورده؛ از منوی راست‌کلیک کشیدنی کنید.';
    if (!part.capabilities || !part.capabilities[`can_stretch_${axis}`]) {
      return 'نوار صاف به‌اندازهٔ کافی بلند در این محور وجود ندارد (قوس/لبهٔ مایل با کشش خراب می‌شود).';
    }
    return null;
  }
  CNC.stretchReason = stretchReason;

  // ================================================================_خطوط اندازه
  const px = () => CNC.viewport.pxPerMM();
  function arrow(parent, x1, y1, x2, y2, color) {
    const dx = x2 - x1, dy = y2 - y1, L = Math.hypot(dx, dy) || 1;
    const ux = dx / L, uy = dy / L, s = 7 / px();
    const wx = -uy, wy = ux;
    el('path', {
      d: `M${x2} ${y2}L${x2 - ux * s * 2.2 + wx * s * .8} ${y2 - uy * s * 2.2 + wy * s * .8}`
       + `M${x2} ${y2}L${x2 - ux * s * 2.2 - wx * s * .8} ${y2 - uy * s * 2.2 - wy * s * .8}`,
      stroke: color, fill: 'none', 'stroke-width': 1.2, 'pointer-events': 'none',
    }, parent);
  }

  function dimLine(parent, opts) {
    // opts: {x1,y1,x2,y2, label, color, dimId, locked, reason, vertical}
    const g = el('g', { class: 'dim-g' + (opts.locked ? ' locked' : ''), 'data-dim': opts.dimId }, parent);
    const stroke = opts.locked ? 'var(--ghost)' : (opts.color || 'var(--accent)');
    el('line', { x1: opts.x1, y1: -opts.y1, x2: opts.x2, y2: -opts.y2,
                 class: 'dimline', stroke, 'stroke-width': 1.3 }, g);
    arrow(g, opts.x1, -opts.y1, opts.x2, -opts.y2, stroke);
    arrow(g, opts.x2, -opts.y2, opts.x1, -opts.y1, stroke);
    // ناحیهٔ کلیک پهن‌تر
    const hit = el('line', { x1: opts.x1, y1: -opts.y1, x2: opts.x2, y2: -opts.y2,
                             stroke: 'transparent', 'stroke-width': 14, 'pointer-events': 'stroke' }, g);
    hit.dataset.dim = opts.dimId;
    if (opts.locked && opts.reason) g.dataset.reason = opts.reason;
    const mx = (opts.x1 + opts.x2) / 2, my = (opts.y1 + opts.y2) / 2;
    const t = el('text', {
      x: opts.vertical ? mx + 8 / px() : mx,
      y: -my + (opts.vertical ? 0 : -10 / px()),
      'font-size': 12 / px(), fill: opts.locked ? 'var(--muted)' : 'var(--text)',
      'text-anchor': 'middle', 'dominant-baseline': opts.vertical ? 'middle' : 'auto',
      direction: 'ltr', 'pointer-events': 'none',
    }, g);
    t.textContent = opts.label;
    return g;
  }

  function drawDims() {
    const gDims = CNC.groups.gDims;
    if (!gDims) return;
    gDims.replaceChildren();
    if (!state.layers.showDims || !state.detection) return;
    const parts = state.detection.parts || [];
    if (!parts.length) return;
    const scene = CNC.unionBBox([...CNC.model.blocks.values()]);
    const off = scene ? Math.max(26, Math.min(scene.x_max - scene.x_min, scene.y_max - scene.y_min) * 0.035) : 26;

    for (const part of parts) {
      const bb = afterBox(part.id) || part.bbox;
      const w = bb.x_max - bb.x_min, h = bb.y_max - bb.y_min;
      const after = state._afterPairs && state._afterPairs.get(part.id);
      const isChanged = !!(after && (Math.abs(after.bbox.x_min - part.bbox.x_min) > 1e-6
        || Math.abs(after.bbox.y_min - part.bbox.y_min) > 1e-6));
      const ry = stretchReason(part, 'y');
      const rx = stretchReason(part, 'x');
      // ارتفاع (Y) — سمت راست قطعه
      dimLine(gDims, {
        x1: bb.x_max + off, y1: bb.y_min, x2: bb.x_max + off, y2: bb.y_max,
        vertical: true, dimId: `stretch:${part.id}:y`,
        label: `${CNC.f1(h)}${isChanged ? ' ★' : ''}`,
        color: isChanged ? 'var(--feat-groove)' : 'var(--accent)',
        locked: !!ry, reason: ry,
      });
      // عرض (X) — پایین قطعه
      dimLine(gDims, {
        x1: bb.x_min, y1: bb.y_min - off, x2: bb.x_max, y2: bb.y_min - off,
        dimId: `stretch:${part.id}:x`,
        label: `${CNC.f1(w)}${isChanged ? ' ★' : ''}`,
        color: isChanged ? 'var(--feat-groove)' : 'var(--accent)',
        locked: !!rx, reason: rx,
      });
    }

    // فاصله‌های بین قطعه‌های مجاور (Y: بالایی نسبت به پایینی، X: راست به چپ)
    const pairsY = adjacentPairs(parts, 'y');
    const pairsX = adjacentPairs(parts, 'x');
    for (const [a, b] of pairsY) {
      drawGapDim(a, b, 'y', off);
    }
    for (const [a, b] of pairsX) {
      drawGapDim(a, b, 'x', off);
    }

    // زنجیرهٔ اندازهٔ بین گروه‌های سوراخ (دادهٔ پنل «طول») — مثل رابط قدیمی
    drawChainDims();
  }

  function adjacentPairs(parts, axis) {
    // جفت‌های مجاور: در هر «ستون/ردیف» (هم‌پوشانی روی محور عرضی)، هر قطعه با
    // نزدیک‌ترین قطعهٔ بعدی روی محور جفت می‌شود. جفت‌کردن صرفاً متوالیِ بعد از
    // sort در چیدمان‌های چندستونی (مثل ۲ ستون × ۴ ردیف) فاصلهٔ بین ردیف‌ها
    // را از دست می‌داد (r1c1↔r2c1 متوالی نیستند چون r1c2 بین‌شان است).
    const lo = axis === 'y' ? 'y_min' : 'x_min';
    const hi = axis === 'y' ? 'y_max' : 'x_max';
    const crossLo = axis === 'y' ? 'x_min' : 'y_min';
    const crossHi = axis === 'y' ? 'x_max' : 'y_max';
    const sorted = parts.slice().sort((p, q) => p.bbox[lo] - q.bbox[lo]);
    const pairs = [];
    const seen = new Set();
    for (let i = 0; i < sorted.length; i++) {
      const a = sorted[i];
      let best = null;
      for (let j = i + 1; j < sorted.length; j++) {
        const b = sorted[j];
        // هم‌پوشانی عرضی (یک ستون/ردیف مشترک) لازم است
        if (Math.min(a.bbox[crossHi], b.bbox[crossHi]) - Math.max(a.bbox[crossLo], b.bbox[crossLo]) <= 0) continue;
        if (!best || b.bbox[lo] < best.bbox[lo]) best = b;
      }
      if (best) {
        const key = `${a.id}|${best.id}`;
        if (!seen.has(key)) { seen.add(key); pairs.push([a, best]); }
      }
    }
    return pairs;
  }

  function drawGapDim(a, b, axis, off) {
    const gDims = CNC.groups.gDims;
    const fa = afterBox(a.id) || a.bbox, fb = afterBox(b.id) || b.bbox;
    const finA = { ...fa }, finB = { ...fb };
    const dia = (part) => (part.tool_dia_used || 0) / 2;
    // bugfix: Math.max(0,…) هم‌پوشانی واقعی را «۰» نشان می‌داد؛ حالا مقدار
    // علامت‌دار محاسبه و در برچسب صادقانه گزارش می‌شود.
    const rawGap = axis === 'y'
      ? fb.y_min - finA.y_max - dia(a) - dia(b)
      : fb.x_min - finA.x_max - dia(a) - dia(b);
    const gapLabel = rawGap >= 0 ? `فاصله ${CNC.f1(rawGap)}` : `هم‌پوشانی ${CNC.f1(-rawGap)}`;
    const lo = axis === 'y' ? 'x_min' : 'y_min';
    const hi = axis === 'y' ? 'x_max' : 'y_max';
    const crossLo = Math.max(finA[lo], fb[lo]);
    const crossHi = Math.min(finA[hi], fb[hi]);
    if (crossHi - crossLo <= 0) return;    // بدون هم‌پوشانی عرضی، اندازه بی‌معناست
    const mid = (crossLo + crossHi) / 2;
    const lockedMsg = (part) => part.locked ? `قطعهٔ ${CNC.partName(part)} قفل است.` : null;
    const reason = lockedMsg(b) || lockedMsg(a) || null;
    if (axis === 'y') {
      const x = finA.x_max + off * 1.9;
      const y1 = Math.min(finA.y_max + dia(a), fb.y_min - dia(b));
      const y2 = Math.max(finA.y_max + dia(a), fb.y_min - dia(b));
      dimLine(gDims, {
        x1: x, y1, x2: x, y2,
        vertical: true, dimId: `gap:${a.id}:${b.id}:y`,
        label: gapLabel, color: 'var(--feat-hole)',
        locked: !!reason, reason,
      });
    } else {
      const y = finA.y_min - off * 1.9;
      const x1 = Math.min(finA.x_max + dia(a), fb.x_min - dia(b));
      const x2 = Math.max(finA.x_max + dia(a), fb.x_min - dia(b));
      dimLine(gDims, {
        x1, y1: y, x2, y2: y,
        dimId: `gap:${a.id}:${b.id}:x`,
        label: gapLabel, color: 'var(--feat-hole)',
        locked: !!reason, reason,
      });
    }
  }
  CNC.drawDims = drawDims;

  // =============================================_زنجیرهٔ اندازه بین گروه‌های سوراخ
  // بازخورد اپراتور: «نمایش اندازه‌های بین سوراخ‌ها و مقدار اضافه‌شدن آن مثل
  // رابط قبلی» — همان زنجیرهٔ per-gap محیط قدیمی: برای هر فاصلهٔ بین دو گروه
  // (لبه↔سوراخ↔سوراخ↔لبه) طول جدید + دلتای رنگی، و طول کل قبل/بعد در سمت چپ.
  // داده از آخرین محاسبهٔ پنل «طول» می‌آید (state.resizeGaps/resizeLengths).
  function drawChainDims() {
    const gDims = CNC.groups.gDims;
    const gaps = state.resizeGaps, lens = state.resizeLengths;
    if (!gDims || !state.layers.showDims || !gaps || !gaps.length || !CNC.model) return;
    const scene = CNC.unionBBox([...CNC.model.blocks.values()]);
    if (!scene) return;
    const p = px();
    const off = Math.max(26, Math.min(scene.x_max - scene.x_min, scene.y_max - scene.y_min) * 0.035);
    const xChain = scene.x_max + off * 3.4;     // زنجیرهٔ per-gap (راست)
    const xAfter = scene.x_min - off * 2.6;     // طول بعد (چپ)
    const xBefore = scene.x_min - off * 3.9;    // طول قبل (چپ‌تر، محو)
    const vdim = (xd, xref, ya, yb, label, delta, color) => {
      if (xref != null) {
        el('line', { x1: xref, y1: -ya, x2: xd, y2: -ya, class: 'chain-ext' }, gDims);
        el('line', { x1: xref, y1: -yb, x2: xd, y2: -yb, class: 'chain-ext' }, gDims);
      }
      el('line', { x1: xd, y1: -ya, x2: xd, y2: -yb, stroke: color, 'stroke-width': 1.3 }, gDims);
      arrow(gDims, xd, -yb, xd, -ya, color);
      arrow(gDims, xd, -ya, xd, -yb, color);
      const t = el('text', { x: xd + 7 / p, y: -(ya + yb) / 2, 'font-size': 11 / p,
                             fill: 'var(--text)', 'dominant-baseline': 'middle',
                             direction: 'ltr', 'pointer-events': 'none', class: 'chain-dim' }, gDims);
      t.textContent = label;
      if (delta != null && Math.abs(delta) > 5e-4) {
        const t2 = el('tspan', { fill: delta < 0 ? 'var(--bad)' : 'var(--good)', dx: 5 / p }, t);
        t2.textContent = `(${CNC.sgn(delta)})`;
      }
    };
    let cum = 0;
    for (const g of gaps) {
      const inc = g.increment || 0;
      const ya = g.y0 + cum, yb = g.y1 + cum + inc;
      cum += inc;
      vdim(xChain, scene.x_max + 4 / p, ya, yb,
           CNC.f1((g.length != null ? g.length : g.y1 - g.y0) + inc), inc,
           Math.abs(inc) > 5e-4 ? 'var(--feat-groove)' : 'var(--muted)');
    }
    if (lens && lens.before != null && lens.after != null) {
      const y0 = gaps[0].y0, y1 = gaps[gaps.length - 1].y1 + cum;
      vdim(xAfter, scene.x_min - 4 / p, y0, y1, CNC.f1(lens.after), null, 'var(--accent)');
      vdim(xBefore, null, y0, y1, CNC.f1(lens.before), null, 'var(--ghost)');
    }
  }

  // ================================================================_ورودی عددی روی خط اندازه
  let dimEditor = null;
  function closeDimEditor() {
    if (dimEditor) { dimEditor.remove(); dimEditor = null; }
    CNC.hideHud();
  }
  CNC.closeDimEditor = closeDimEditor;

  function openDimEditor(dimId, client) {
    closeDimEditor();
    const [kind, partId, axis] = dimId.split(':');
    const part = findPart(partId);
    if (!part) return;
    if (kind === 'gap') {
      openGapEditor(dimId, partId, axis, client);
      return;
    }
    if (partEditBlocked(client)) return;
    const reason = stretchReason(part, axis);
    if (reason) {
      // چرا قابل‌ویرایش نیست — روی خودِ شیء
      showTransient(client, `<b>${CNC.partName(part)}</b> — ${esc(reason)}`);
      return;
    }
    const bb = beforeBox(partId);
    const current = axis === 'y' ? bb.y_max - bb.y_min : bb.x_max - bb.x_min;
    // بازخورد اپراتور: ورودی عددی نباید وقتی فرزند روی نوار پیش‌فرض است کلاً رد
    // شود — نوار به نزدیک‌ترین فاصلهٔ خالی می‌رود (مثل درگ)؛ فقط وقتی هیچ فاصلهٔ
    // خالی نیست قفل است و کاربر به پنل «طول» هدایت می‌شود.
    const plan = bandPlan(part, axis);
    if (plan && plan.hard) {
      showTransient(client, `<b>${CNC.partName(part)}</b> — ${esc(plan.note)}`);
      return;
    }
    openEditor(client, {
      title: `${CNC.partName(part)} — ${axis === 'y' ? 'ارتفاع' : 'عرض'} جدید`,
      value: CNC.f1(current),
      hint: (plan && plan.note) ? plan.note
        : 'اندازهٔ مطلق کانتور (mm) — نوار صاف کشیده می‌شود و بخش‌های بالای نوار جابه‌جا می‌شوند',
      onApply: (value) => {
        const newSize = Number(value);
        if (!Number.isFinite(newSize) || newSize <= 0 || Math.abs(newSize - current) < 1e-9) return;
        const op = { op: 'stretch_part', part_id: partId, axis,
                     new_size: Math.round(newSize * 1000) / 1000 };
        if (plan && plan.distribution) op.distribution = 'gaps';
        else if (plan) op.band = plan.band;
        commitOp(partId, op,
                 `کشش ${CNC.partName(part)} ${axis.toUpperCase()}`);
      },
    });
  }

  function openGapEditor(dimId, partId, axis, client) {
    const [, aId, bId] = dimId.split(':');
    const a = findPart(aId), b = findPart(bId);
    if (!a || !b) return;
    if (partEditBlocked(client)) return;
    if (a.locked || b.locked) {
      showTransient(client, `فاصلهٔ این جفت قابل تغییر نیست — ${esc(a.locked ? CNC.partName(a) : CNC.partName(b))} قفل است (کانتورهای هم‌پوشان).`);
      return;
    }
    const dia = (p) => (p.tool_dia_used || 0) / 2;
    const fa = afterBox(aId) || a.bbox, fb = afterBox(bId) || b.bbox;
    // prefill علامت‌دار: هم‌پوشانی واقعی «۰» نمایش داده نمی‌شود؛ اعمال هنوز
    // فقط gap نامنفی می‌پذیرد (سرور هم منفی را رد می‌کند).
    const gap = axis === 'y' ? fb.y_min - fa.y_max - dia(a) - dia(b)
                             : fb.x_min - fa.x_max - dia(a) - dia(b);
    openEditor(client, {
      title: `فاصلهٔ ${CNC.partName(a)} ↔ ${CNC.partName(b)} (${axis.toUpperCase()})`,
      value: CNC.f1(gap),
      hint: 'فاصلهٔ لبه‌به‌لبهٔ نهایی (با احتساب قطر ابزار) — قطعهٔ جلوتر جابه‌جا می‌شود',
      onApply: (value) => {
        const newGap = Number(value);
        if (!Number.isFinite(newGap) || newGap < 0 || Math.abs(newGap - gap) < 1e-9) return;
        // یک op برای هر قطعه: set_gap جایش op قطعهٔ jلوئی (b) را می‌گیرد
        state.partOp.delete(bId);
        commitOp(bId, { op: 'set_gap', part_a: aId, part_b: bId, axis,
                        gap: Math.round(newGap * 1000) / 1000 },
                 `فاصلهٔ ${CNC.partName(a)}↔${CNC.partName(b)}`);
      },
    });
  }

  function openEditor(client, opts) {
    dimEditor = document.createElement('div');
    dimEditor.className = 'dim-editor';
    dimEditor.setAttribute('role', 'dialog');
    dimEditor.innerHTML = `
      <label style="font-size:11.5px;color:var(--muted)">${esc(opts.title)}
        <input type="number" step="0.1" value="${esc(opts.value)}" aria-label="${esc(opts.title)}"></label>
      <span class="unit">mm</span>
      <button type="button" class="ok primary">اعمال ⏎</button>`;
    document.body.appendChild(dimEditor);
    const input = dimEditor.querySelector('input');
    const rect = dimEditor.getBoundingClientRect();
    let x = client.x - rect.width / 2, y = client.y - rect.height - 12;
    x = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
    y = Math.max(60, y);
    dimEditor.style.left = x + 'px';
    dimEditor.style.top = y + 'px';
    input.focus(); input.select();
    const apply = () => {
      const v = input.value;
      closeDimEditor();
      opts.onApply(v);
    };
    dimEditor.querySelector('.ok').onclick = apply;
    input.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); apply(); }
      if (e.key === 'Escape') { e.stopPropagation(); closeDimEditor(); }
    };
    const hint = opts.hint ? opts.hint : '';
    showTransient({ x, y: y + 44 }, esc(hint));
  }

  let transientEl = null, transientTimer = null;
  function showTransient(client, html) {
    if (transientEl) transientEl.remove();
    transientEl = document.createElement('div');
    transientEl.className = 'maphud dim-hint';
    transientEl.innerHTML = html;
    document.body.appendChild(transientEl);
    const r = transientEl.getBoundingClientRect();
    transientEl.style.left = Math.min(client.x, window.innerWidth - r.width - 10) + 'px';
    transientEl.style.top = Math.min(client.y, window.innerHeight - r.height - 10) + 'px';
    clearTimeout(transientTimer);
    transientTimer = setTimeout(() => { transientEl && transientEl.remove(); transientEl = null; }, 4200);
  }
  CNC.showTransient = showTransient;

  CNC.on('map-click', (info) => {
    if (!info.dimId) return;
    openDimEditor(info.dimId, info.client);
  });

  // ================================================================_درگ جابه‌جایی
  let moveDrag = null;   // {partId, startX, startY, dx, dy, snapped}
  let liveDimEls = [];

  function snapCandidates(partId, exclude) {
    const out = [];
    for (const part of state.detection.parts || []) {
      if (part.id === exclude) continue;
      const b = part.bbox;
      out.push({ x: b.x_min, y: b.y_min }, { x: b.x_max, y: b.y_min },
               { x: b.x_min, y: b.y_max }, { x: b.x_max, y: b.y_max },
               { x: (b.x_min + b.x_max) / 2, y: (b.y_min + b.y_max) / 2 });
    }
    return out;
  }

  function applySnap(dragged, dx, dy, partId) {
    const tol = 10 / px();
    const b = dragged;   // کادر متحرک فعلی
    const cands = snapCandidates(partId, partId);
    const myXs = [b.x_min, b.x_max, (b.x_min + b.x_max) / 2];
    const myYs = [b.y_min, b.y_max, (b.y_min + b.y_max) / 2];
    let bestX = null, bestY = null;
    for (const c of cands) {
      for (const mx of myXs) {
        const d = c.x - mx;
        if (Math.abs(d) <= tol && (bestX === null || Math.abs(d) < Math.abs(bestX.d))) {
          bestX = { d, at: c.x };
        }
      }
      for (const my of myYs) {
        const d = c.y - my;
        if (Math.abs(d) <= tol && (bestY === null || Math.abs(d) < Math.abs(bestY.d))) {
          bestY = { d, at: c.y };
        }
      }
    }
    return { dx: dx + (bestX ? bestX.d : 0), dy: dy + (bestY ? bestY.d : 0),
             snapX: bestX, snapY: bestY };
  }

  function drawLiveGaps(partId, box) {
    liveDimEls.forEach((e) => e.remove()); liveDimEls = [];
    const part = findPart(partId);
    if (!part) return;
    const dia = (p) => (p.tool_dia_used || 0) / 2;
    for (const other of state.detection.parts || []) {
      if (other.id === partId) continue;
      const ob = other.bbox;
      // هم‌پوشانی عرضی → فاصلهٔ زنده روی محور
      if (ob.x_min < box.x_max && ob.x_max > box.x_min) {
        const below = ob.y_max <= box.y_min, above = ob.y_min >= box.y_max;
        if (below || above) {
          const x = (Math.max(ob.x_min, box.x_min) + Math.min(ob.x_max, box.x_max)) / 2;
          const g = el('g', { class: 'dim-g', 'pointer-events': 'none' }, CNC.groups.gHud);
          const yA = below ? box.y_min : box.y_max;
          const yB = below ? ob.y_max : ob.y_min;
          const gap = below ? box.y_min - ob.y_max - dia(part) - dia(other)
                            : ob.y_min - box.y_max - dia(part) - dia(other);
          el('line', { x1: x, y1: -yA, x2: x, y2: -yB, stroke: 'var(--feat-hole)', 'stroke-width': 1.2 }, g);
          const t = el('text', { x: x + 8 / px(), y: -(yA + yB) / 2, 'font-size': 11.5 / px(),
                                 fill: 'var(--feat-hole)', direction: 'ltr', 'dominant-baseline': 'middle' }, g);
          t.textContent = CNC.f1(Math.max(0, gap));
          liveDimEls.push(g);
        }
      }
      if (ob.y_min < box.y_max && ob.y_max > box.y_min) {
        const left = ob.x_max <= box.x_min, right = ob.x_min >= box.x_max;
        if (left || right) {
          const y = (Math.max(ob.y_min, box.y_min) + Math.min(ob.y_max, box.y_max)) / 2;
          const g = el('g', { class: 'dim-g', 'pointer-events': 'none' }, CNC.groups.gHud);
          const xA = left ? box.x_min : box.x_max;
          const xB = left ? ob.x_max : ob.x_min;
          const gap = left ? box.x_min - ob.x_max - dia(part) - dia(other)
                           : ob.x_min - box.x_max - dia(part) - dia(other);
          el('line', { x1: xA, y1: -y, x2: xB, y2: -y, stroke: 'var(--feat-hole)', 'stroke-width': 1.2 }, g);
          const t = el('text', { x: (xA + xB) / 2, y: -y - 8 / px(), 'font-size': 11.5 / px(),
                                 fill: 'var(--feat-hole)', 'text-anchor': 'middle', direction: 'ltr' }, g);
          t.textContent = CNC.f1(Math.max(0, gap));
          liveDimEls.push(g);
        }
      }
    }
  }

  function liveMovePreview(partId, dx, dy) {
    const ref = CNC.viewEls.refs[partId];
    if (!ref) return;
    ref.g.setAttribute('transform', `translate(${dx} ${-dy})`);
    const box = { ...beforeBox(partId) };
    box.x_min += dx; box.x_max += dx; box.y_min += dy; box.y_max += dy;
    drawLiveGaps(partId, box);
    const hud = document.getElementById('liveHud');
    if (hud) {
      hud.hidden = false;
      hud.textContent = `ΔX ${CNC.f1(dx)}   ΔY ${CNC.f1(dy)} mm — رها کنید تا پیش‌نمایش سرور بیاید`;
    }
  }

  CNC.on('map-down', (info) => {
    if (info.claimed || info.button !== 0) return;
    if (state.tool !== 'select') return;
    // اولویت: دستهٔ تغییر اندازه → نوار کشش → درگ قطعه
    if (info.handle) { startHandleDrag(info); return; }
    if (info.bar) { startBarDrag(info); return; }
    // بازخورد اپراتور: درگ از روی مسیر عملیاتِ متعلق به قطعه (راستر سرپنتاین
    // پنل خم‌کاری) هم باید درگ همان قطعه باشد — وگرنه از هیچ‌جای قطعه نمی‌شد
    // درگش کرد چون همهٔ سطحش پوشیده از مسیر عملیات است.
    let downPartId = info.partId;
    if (!downPartId && info.opId) {
      const op = ((state.detection && state.detection.operations) || []).find((o) => o.id === info.opId);
      downPartId = op && op.assigned_part;
    }
    if (downPartId && !info.dimId) {
      if (partEditBlocked(info.client)) return;
      const part = findPart(downPartId);
      if (!part || part.locked) {
        if (part && part.locked && info.pointerType === 'mouse') {
          CNC.showTransient(info.client, `<b>${CNC.partName(part)}</b> قفل است — ${esc(part.warnings?.[0] || 'کانتورهای هم‌پوشان')}`);
        }
        return;
      }
      if (!part.capabilities || !part.capabilities.can_translate) return;
      if (state.partOp.get(downPartId)?.op === 'stretch_part') {
        CNC.showTransient(info.client, 'درگ، کشش قبلی این قطعه را جایگزین می‌کند (هر قطعه یک تبدیل)؛ اگر نمی‌خواهید، Esc یا واگرد کنید.');
      }
      state._gestureClaimed = true;
      moveDrag = { partId: downPartId, startWorld: info.world, dx: 0, dy: 0, shift: info.shift };
      CNC.selection.set([{ type: 'part', id: downPartId }]);
    }
  });

  CNC.on('map-move', (info) => {
    if (moveDrag) {
      const raw = { dx: info.world.x - moveDrag.startWorld.x,
                    dy: info.world.y - moveDrag.startWorld.y };
      const base = beforeBox(moveDrag.partId);
      const moved = { ...base };
      moved.x_min += raw.dx; moved.x_max += raw.dx;
      moved.y_min += raw.dy; moved.y_max += raw.dy;
      const snapped = applySnap(moved, raw.dx, raw.dy, moveDrag.partId);
      moveDrag.dx = snapped.dx; moveDrag.dy = snapped.dy;
      moveDrag.snapX = snapped.snapX; moveDrag.snapY = snapped.snapY;
      liveMovePreview(moveDrag.partId, moveDrag.dx, moveDrag.dy);
      drawSnapLines(moveDrag.snapX, moveDrag.snapY);
      return;
    }
    if (handleDrag) { moveHandleDrag(info); return; }
    if (barDrag) { moveBarDrag(info); return; }
  });

  function drawSnapLines(snapX, snapY) {
    const g = CNC.groups.gHud;
    g.querySelectorAll('.snapline').forEach((e) => e.remove());
    const scene = CNC.unionBBox([...CNC.model.blocks.values()]);
    if (!scene) return;
    if (snapX) {
      el('line', { x1: snapX.at, y1: -scene.y_max - 40, x2: snapX.at, y2: -scene.y_min + 40,
                   class: 'snapline', 'pointer-events': 'none' }, g);
    }
    if (snapY) {
      el('line', { x1: scene.x_min - 40, y1: -snapY.at, x2: scene.x_max + 40, y2: -snapY.at,
                   class: 'snapline', 'pointer-events': 'none' }, g);
    }
  }

  CNC.on('map-up', (info) => {
    if (moveDrag) {
      const drag = moveDrag; moveDrag = null;
      clearLive();
      if (Math.abs(drag.dx) < 1e-9 && Math.abs(drag.dy) < 1e-9) return;
      const applied = appliedDelta(drag.partId);
      const dx = Math.round((applied.dx + drag.dx) * 1000) / 1000;
      const dy = Math.round((applied.dy + drag.dy) * 1000) / 1000;
      if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9) return;
      commitOp(drag.partId, { op: 'move_part', part_id: drag.partId, dx, dy },
               `جابه‌جایی ${CNC.partName(findPart(drag.partId) || {})} (${CNC.sgn(dx)},${CNC.sgn(dy)})`);
    }
    if (handleDrag) { finishHandleDrag(); }
    if (barDrag) { finishBarDrag(); }
  });

  function clearLive() {
    Object.values(CNC.viewEls.refs).forEach((r) => r.g.removeAttribute('transform'));
    CNC.groups.gHud.querySelectorAll('.snapline').forEach((e) => e.remove());
    liveDimEls.forEach((e) => e.remove()); liveDimEls = [];
    const hud = document.getElementById('liveHud');
    if (hud) hud.hidden = true;
  }
  CNC.clearLive = clearLive;

  // ================================================================_نوارهای کشش
  let barDrag = null;    // {partId, axis, band, startWorld, delta}
  function drawStretchBars() {
    const g = CNC.groups.gStretch;
    if (!g) return;
    g.replaceChildren();
    if (!state.layers.showDims) return;
    const sel = state.selection.find((s) => s.type === 'part');
    if (!sel) return;
    const part = findPart(sel.id);
    if (!part || part.locked) return;
    const bb = afterBox(part.id) || part.bbox;
    for (const axis of ['y', 'x']) {
      if (stretchReason(part, axis)) continue;     // فقط قطعه‌های واقعاً کشش‌پذیر
      // نوار «نهایی» کشش: اگر فرزند روی نوار پیش‌فرض باشد، همان‌جا قفل نمی‌کنیم —
      // نوارِ جابه‌جاشده به نزدیک‌ترین فاصلهٔ خالی رسم می‌شود (هماهنگ با سرور)
      const plan = bandPlan(part, axis);
      if (!plan) continue;
      const band = plan.band;
      const color = plan.hard ? 'lockseg' : 'okseg';
      const note = plan.hard ? plan.note
        : (plan.relocated ? plan.relocated
          : 'کشیدن این لبه قطعه را در این محور بلند می‌کند؛ لنگرِ پایین ثابت می‌ماند.');
      if (axis === 'y') {
        const g1 = el('g', { class: 'stretchbar', 'data-bar': `band:${part.id}:y:free` }, g);
        el('line', { x1: bb.x_min, y1: -band[1], x2: bb.x_max, y2: -band[1],
                     class: color }, g1);
        const hit = el('line', { x1: bb.x_min, y1: -band[1], x2: bb.x_max, y2: -band[1],
                                 stroke: 'transparent', 'stroke-width': 16,
                                 'pointer-events': 'stroke' }, g1);
        hit.dataset.bar = `band:${part.id}:y:free`;
        g1.dataset.reason = note;
        // لنگر (پایین)
        const g2 = el('g', { class: 'stretchbar', 'data-bar': `band:${part.id}:y:anchor` }, g);
        el('line', { x1: bb.x_min, y1: -band[0], x2: bb.x_max, y2: -band[0], class: 'lockseg' }, g2);
        const hit2 = el('line', { x1: bb.x_min, y1: -band[0], x2: bb.x_max, y2: -band[0],
                                  stroke: 'transparent', 'stroke-width': 16,
                                  'pointer-events': 'stroke' }, g2);
        hit2.dataset.bar = `band:${part.id}:y:anchor`;
        g2.dataset.reason = 'این لبه لنگر است؛ بخش زیر نوار ثابت می‌ماند و کشش از همین‌جا اندازه گرفته می‌شود.';
      } else {
        const g1 = el('g', { class: 'stretchbar', 'data-bar': `band:${part.id}:x:free` }, g);
        el('line', { x1: band[1], y1: -bb.y_min, x2: band[1], y2: -bb.y_max, class: color }, g1);
        const hit = el('line', { x1: band[1], y1: -bb.y_min, x2: band[1], y2: -bb.y_max,
                                 stroke: 'transparent', 'stroke-width': 16,
                                 'pointer-events': 'stroke' }, g1);
        hit.dataset.bar = `band:${part.id}:x:free`;
        g1.dataset.reason = note;
        const g2 = el('g', { class: 'stretchbar', 'data-bar': `band:${part.id}:x:anchor` }, g);
        el('line', { x1: band[0], y1: -bb.y_min, x2: band[0], y2: -bb.y_max, class: 'lockseg' }, g2);
        const hit2 = el('line', { x1: band[0], y1: -bb.y_min, x2: band[0], y2: -bb.y_max,
                                  stroke: 'transparent', 'stroke-width': 16,
                                  'pointer-events': 'stroke' }, g2);
        hit2.dataset.bar = `band:${part.id}:x:anchor`;
        g2.dataset.reason = 'این لبه لنگر است؛ بخش سمت دیگر نوار ثابت می‌ماند.';
      }
    }
  }
  function bandPlan(part, axis) {
    // بازخورد اپراتور: کشش روی پنل سوراخ‌دار نباید کلاً رد شود — برش کشش
    // مثل روش قدیمی در نزدیک‌ترین فاصلهٔ خالی بین گروه‌های سوراخ می‌نشیند
    // (همان منطقی که سرور در part_edits._relocate_band اجرا می‌کند).
    const lo = `${axis}_min`, hi = `${axis}_max`;
    const band = part[`stretch_band_${axis}`];
    if (!band) return null;
    // بازخورد اپراتور (t.16.2180x385): قطعهٔ سوراخ‌دار (ردیف سوراخ + شیار
    // طولی) مثل روش قدیمی توزیع می‌شود — طول اضافه بین فاصلهٔ گروه‌های سوراخ
    // می‌نشیند و شیار طولی همراه قطعه بلند می‌شود؛ برش تک‌نواری لازم ندارد.
    // band فقط برای نمایش لبهٔ آزاد/لنگر می‌ماند.
    const distLevels = part[`row_levels_${axis}`] || [];
    if (part.capabilities && part.capabilities[`can_distribute_${axis}`]
        && distLevels.length >= 1 && part[`distribute_span_${axis}`]) {
      return { band, relocated: null, hard: false, distribution: true,
               note: `طول اضافه بین ${distLevels.length + 1} فاصلهٔ گروه‌های سوراخ توزیع می‌شود (مثل روش قدیمی)؛ شیار طولی هم بلند می‌شود.` };
    }
    const straddles = (b, bnd) => !(b[hi] <= bnd[0] + 1e-6 || b[lo] >= bnd[1] - 1e-6);
    const childBoxes = (part.children || []).filter((c) => c.bbox).map((c) => c.bbox);
    let final = band, relocated = null;
    if (childBoxes.some((b) => straddles(b, band))) {
      const intervals = (part[`stretch_bands_${axis}`] || [band]).map((b) => [b[0], b[1]]);
      const margin = 0.25, minClear = 0.5;
      const blocked = childBoxes.map((b) => [b[lo] - margin, b[hi] + margin]).sort((p, q) => p[0] - q[0]);
      const center0 = (band[0] + band[1]) / 2;
      const cands = [];
      for (const interval of intervals) {
        let cursor = interval[0];
        for (const [a, b] of blocked) {
          if (b <= cursor) continue;
          if (a > cursor) cands.push([cursor, Math.min(a, interval[1])]);
          cursor = Math.max(cursor, b);
          if (cursor >= interval[1]) break;
        }
        if (interval[1] - cursor > 0) cands.push([cursor, interval[1]]);
      }
      const clear = cands.filter(([a, b]) => b - a >= minClear);
      if (!clear.length) {
        return { band, relocated: null, hard: true,
                 note: `هیچ فاصلهٔ خالی بین فرزندها برای برش کشش نیست (مثل راستر/شیار تمام‌صفحه) — تغییر طول این قطعه را از پنل «طول» (توزیع بین فاصله‌ها یا kerf) انجام دهید.` };
      }
      clear.sort((p, q) => Math.abs((p[0] + p[1]) / 2 - center0) - Math.abs((q[0] + q[1]) / 2 - center0));
      const nb = clear[0];
      final = [Math.round(nb[0] * 1000) / 1000, Math.round(nb[1] * 1000) / 1000];
      relocated = `برش کشش به نزدیک‌ترین فاصلهٔ خالی بین گروه‌ها منتقل می‌شود (${CNC.f1(final[0])}–${CNC.f1(final[1])} mm).`;
    }
    for (const other of state.detection.parts || []) {
      if (other.id === part.id) continue;
      if (straddles(other.bbox, final)) {
        return { band: final, relocated, hard: true,
                 note: `قطعهٔ ${CNC.partName(other)} روی نوار کشش قرار دارد و کشیدن آن را پاره می‌کند.` };
      }
    }
    return { band: final, relocated, hard: false, note: relocated };
  }

  function startBarDrag(info) {
    const [, partId, axis, edge] = info.bar.split(':');
    const part = findPart(partId);
    if (!part) return;
    if (partEditBlocked(info.client)) return;
    if (edge === 'anchor') {
      CNC.showTransient(info.client, 'این لبه لنگر است؛ بخش زیر/سمتِ دیگر نوار ثابت می‌ماند و کشش با لبهٔ آزاد انجام می‌شود.');
      return;
    }
    const plan = bandPlan(part, axis);
    if (!plan) return;
    if (plan.hard) {
      CNC.showTransient(info.client, `<b>${CNC.partName(part)}</b> — ${esc(plan.note)}`);
      return;
    }
    if (plan.note) CNC.showTransient(info.client, esc(plan.note));
    state._gestureClaimed = true;
    barDrag = { partId, axis, band: plan.band, distribution: !!plan.distribution,
                start: axis === 'y' ? info.world.y : info.world.x, delta: 0 };
    CNC.selection.set([{ type: 'part', id: partId }]);
  }
  function moveBarDrag(info) {
    if (!barDrag) return;
    const cur = barDrag.axis === 'y' ? info.world.y : info.world.x;
    barDrag.delta = cur - barDrag.start;
    liveStretchPreview(barDrag.partId, barDrag.axis, barOffsetAt(barDrag, barDrag.delta));
    const hud = document.getElementById('liveHud');
    if (hud) {
      hud.hidden = false;
      hud.textContent = `کشش ${barDrag.axis.toUpperCase()}: ${CNC.sgn(barDrag.delta)} mm — رها کنید تا پیش‌نمایش سرور بیاید`;
    }
  }
  function finishBarDrag() {
    const drag = barDrag; barDrag = null;
    clearLive();
    restoreLiveStretch();
    if (!drag || Math.abs(drag.delta) < 1e-9) return;
    const applied = appliedStretch(drag.partId, drag.axis);
    const delta = Math.round((applied + drag.delta) * 1000) / 1000;
    if (Math.abs(delta) < 1e-9) return;
    state.partOp.delete(drag.partId);
    // band صریح فقط برای کشش تک‌نواری؛ حالت توزیع (روش قدیمی) بدون band می‌رود
    // تا سرور همان نگاشت چندمنطقه‌ای را اجرا کند که پیش‌نمایش زنده نشان داد.
    const op = { op: 'stretch_part', part_id: drag.partId, axis: drag.axis,
                 delta: Math.round(delta * 1000) / 1000 };
    if (drag.distribution) op.distribution = 'gaps';
    else op.band = drag.band;
    commitOp(drag.partId, op,
             `کشش ${CNC.partName(findPart(drag.partId) || {})} ${drag.axis.toUpperCase()} ${CNC.sgn(delta)}`);
  }

  // تابع آفست اتصال‌ها: تک‌نواری (زیر نوار ۰، بالای آن دلتا) یا توزیع چندمنطقه‌ای
  function barOffsetAt(drag, delta) {
    const part = findPart(drag.partId);
    if (drag.distribution && part) return zoneOffsetAt(part, drag.axis, delta);
    return (v) => (v <= drag.band[0] + 1e-6 ? 0 : v >= drag.band[1] - 1e-6 ? delta : 0);
  }

  // نگاشت توزیع سمت کلاینت — همان features/cuts سرور (_distribution_plan)
  function zoneOffsetAt(part, axis, delta) {
    const span = part[`distribute_span_${axis}`];
    const levels = (part[`row_levels_${axis}`] || [])
      .filter((v) => v > span[0] + 1e-6 && v < span[1] - 1e-6);
    const features = [span[0], ...levels, span[1]];
    const n = features.length - 1;
    if (n < 1 || !Number.isFinite(delta) || Math.abs(delta) < 1e-9) return () => 0;
    const inc = delta / n;
    const cuts = [];
    for (let i = 0; i < n; i++) cuts.push((features[i] + features[i + 1]) / 2);
    return (v) => {
      let off = 0;
      for (const c of cuts) {
        if (v > c + 1e-6) off += inc; else break;
      }
      return off;
    };
  }

  // جابه‌جایی اعمال‌شدهٔ فعلی روی «بالای نوار» = فرق مرکز بالای قطعه قبل/بعد
  function appliedStretch(partId, axis) {
    const after = state._afterPairs && state._afterPairs.get(partId);
    const before = findPart(partId);
    if (!after || !before) return 0;
    return axis === 'y'
      ? after.bbox.y_max - before.bbox.y_max
      : after.bbox.x_max - before.bbox.x_max;
  }

  function liveStretchPreview(partId, axis, offsetAt) {
    const ref = CNC.viewEls.refs[partId];
    if (!ref) return;
    const part = findPart(partId);
    if (!part) return;
    // بازسازی d مسیرهای قطعه با تابع آفست همان قرارداد سرور
    ref.g.querySelectorAll('path[data-block]').forEach((p) => {
      const block = CNC.model.blocks.get(p.dataset.block);
      if (!block) return;
      if (!p.dataset.origD) p.dataset.origD = p.getAttribute('d');
      p.setAttribute('d', stretchBlockD(block, axis, offsetAt));
    });
  }
  function stretchBlockD(block, axis, offsetAt) {
    let d = '';
    for (const s of block.segments) {
      const c1 = axis === 'y' ? s.y1 : s.x1;
      const c2 = axis === 'y' ? s.y2 : s.x2;
      const o1 = offsetAt(c1), o2 = offsetAt(c2);
      let x1 = s.x1, y1 = s.y1, x2 = s.x2, y2 = s.y2;
      if (axis === 'y') { y1 += o1; y2 += o2; } else { x1 += o1; x2 += o2; }
      if (s.t === 'line') {
        d += `M${x1} ${-y1}L${x2} ${-y2}`;
      } else {
        // قوس فقط وقتی سالم می‌ماند که هر دو سر آفست برابر داشته باشند
        // (انتقال صلب)؛ وگرنه سرور همین درخواست را رد می‌کند.
        d += `M${x1} ${-y1}A${s.r} ${s.r} 0 ${s.large ? 1 : 0} ${s.sweep} ${x2} ${-y2}`;
      }
    }
    return d;
  }
  function restoreLiveStretch() {
    document.querySelectorAll('#gParts path[data-orig-d]').forEach((p) => {
      p.setAttribute('d', p.dataset.origD);
      delete p.dataset.origD;
    });
  }

  // ================================================================_دسته‌های تغییر اندازه
  let handleDrag = null;  // {partId, axis, edge, start, size0}
  function drawHandles() {
    const g = CNC.groups.gHandles;
    if (!g) return;
    g.replaceChildren();
    if (!state.layers.showDims) return;
    const sel = state.selection.find((s) => s.type === 'part');
    if (!sel) return;
    const part = findPart(sel.id);
    if (!part || part.locked) return;
    const bb = afterBox(part.id) || part.bbox;
    const s = 7 / px();
    const mk = (cx, cy, key, locked, reason) => {
      const h = el('rect', {
        x: cx - s, y: -cy - s, width: s * 2, height: s * 2, rx: 2,
        class: 'handle' + (locked ? ' locked' : ''), 'data-handle': key,
      }, g);
      if (reason) h.dataset.reason = reason;
      return h;
    };
    const ry = stretchReason(part, 'y'), rx = stretchReason(part, 'x');
    const anchorNoteY = 'لبهٔ پایینی لنگر ماشین کشش است؛ در یک تبدیل قابل کشش نیست — لبهٔ مقابل را بکشید.';
    const anchorNoteX = 'لبهٔ چپی لنگر ماشین کشش است؛ در یک تبدیل قابل کشش نیست — لبهٔ مقابل را بکشید.';
    // لبهٔ آزاد (max) — فقط وقتی همان محور کشش‌پذیر است
    mk((bb.x_min + bb.x_max) / 2, bb.y_max, `${part.id}:y:max`, !!ry, ry);
    mk(bb.x_max, (bb.y_min + bb.y_max) / 2, `${part.id}:x:max`, !!rx, rx);
    // لبهٔ لنگر (min) — همیشه قفل با توضیح صادقانه
    mk((bb.x_min + bb.x_max) / 2, bb.y_min, `${part.id}:y:min`, true,
       ry || anchorNoteY);
    mk(bb.x_min, (bb.y_min + bb.y_max) / 2, `${part.id}:x:min`, true,
       rx || anchorNoteX);
    // گوشه‌ها: تغییر دو محور هم‌زمان در یک درخواست ممکن نیست — قفل با توضیح
    const corner = 'تغییر هم‌زمان دو محور در یک تبدیل پشتیبانی نمی‌شود؛ هر لبه را جداگانه بکشید.';
    for (const [cx, cy] of [[bb.x_min, bb.y_min], [bb.x_max, bb.y_min],
                            [bb.x_min, bb.y_max], [bb.x_max, bb.y_max]]) {
      mk(cx, cy, `corner:${part.id}`, true, corner);
    }
  }

  function startHandleDrag(info) {
    const key = info.handle;
    if (key.startsWith('corner:')) {
      CNC.showTransient(info.client, 'تغییر هم‌زمان دو محور در یک تبدیل پشتیبانی نمی‌شود؛ هر لبه را جداگانه بکشید.');
      return;
    }
    const [partId, axis, edge] = key.split(':');
    const part = findPart(partId);
    if (!part) return;
    if (partEditBlocked(info.client)) return;
    // ماشین کشش سرور لنگر را زیر/سمت دیگر نوار می‌گیرد؛ لبهٔ min (پایین/چپ)
    // در یک تبدیل قابل کشش نیست — صادقانه قفل می‌شود.
    if (edge === 'min') {
      CNC.showTransient(info.client, `لبهٔ ${axis === 'y' ? 'پایینی' : 'چپی'} لنگر ماشین کشش است؛ در یک تبدیل قابل کشش نیست — لبهٔ مقابل را بکشید.`);
      return;
    }
    const reason = stretchReason(part, axis);
    if (reason) {
      CNC.showTransient(info.client, `<b>${CNC.partName(part)}</b> — ${esc(reason)}`);
      return;
    }
    const plan = bandPlan(part, axis);
    if (!plan) {
      CNC.showTransient(info.client, 'نوار صافی برای کشش این لبه نیست (قوس/لبهٔ مایل).');
      return;
    }
    if (plan.hard) { CNC.showTransient(info.client, esc(plan.note)); return; }
    if (plan.note) CNC.showTransient(info.client, esc(plan.note));
    state._gestureClaimed = true;
    handleDrag = {
      partId, axis, edge, band: plan.band, distribution: !!plan.distribution,
      start: axis === 'y' ? info.world.y : info.world.x,
      size0: axis === 'y'
        ? (beforeBox(partId).y_max - beforeBox(partId).y_min)
        : (beforeBox(partId).x_max - beforeBox(partId).x_min),
    };
    CNC.selection.set([{ type: 'part', id: partId }]);
  }
  function moveHandleDrag(info) {
    if (!handleDrag) return;
    const cur = handleDrag.axis === 'y' ? info.world.y : info.world.x;
    const raw = cur - handleDrag.start;
    const sign = handleDrag.edge === 'max' ? 1 : -1;
    const newSize = Math.max(1, handleDrag.size0 + sign * raw);
    const hud = document.getElementById('liveHud');
    if (hud) {
      hud.hidden = false;
      hud.textContent = `${handleDrag.axis.toUpperCase()}: ${CNC.f1(handleDrag.size0)} → ${CNC.f1(newSize)} mm`;
    }
    handleDrag.newSize = newSize;
  }
  function finishHandleDrag() {
    const drag = handleDrag; handleDrag = null;
    clearLive();
    if (!drag || !drag.newSize
        || Math.abs(drag.newSize - drag.size0) < 1e-9) return;
    state.partOp.delete(drag.partId);
    const op = { op: 'stretch_part', part_id: drag.partId, axis: drag.axis,
                 new_size: Math.round(drag.newSize * 1000) / 1000 };
    if (drag.distribution) op.distribution = 'gaps';
    else op.band = drag.band;
    commitOp(drag.partId, op,
             `تغییر اندازهٔ ${CNC.partName(findPart(drag.partId) || {})} ${drag.axis.toUpperCase()}`);
  }

  function selectedChainParts() {
    const ids = [...new Set(state.selection.filter((item) => item.type === 'part').map((item) => item.id))];
    return ids.map((id) => findPart(id)).filter(Boolean);
  }
  function updateChainSelectionHint() {
    const hint = document.getElementById('chainStretchSelection');
    if (!hint) return;
    const parts = selectedChainParts();
    hint.textContent = parts.length >= 2
      ? `انتخاب شده: ${parts.map((part) => CNC.partName(part)).join(' ← ')}؛ ترتیب نهایی با محور و مختصات قطعات تعیین می‌شود.`
      : 'حداقل دو قطعه را با Shift+کلیک انتخاب کنید.';
  }
  document.getElementById('chainStretchApply')?.addEventListener('click', () => {
    if (partEditBlocked()) return;
    const parts = selectedChainParts();
    if (parts.length < 2) {
      CNC.alert('warn', 'برای کشش زنجیره‌ای حداقل دو قطعه را با Shift+کلیک انتخاب کنید.');
      return;
    }
    const locked = parts.find((part) => part.locked || state.fixedParts.has(part.id));
    if (locked) {
      CNC.alert('warn', `قطعهٔ ${CNC.partName(locked)} قفل/ثابت است؛ ابتدا آن را کشیدنی کنید.`);
      return;
    }
    const axis = document.getElementById('chainStretchAxis')?.value || 'y';
    const delta = Number(document.getElementById('chainStretchDelta')?.value);
    if (!Number.isFinite(delta) || Math.abs(delta) < 1e-9) {
      CNC.alert('warn', 'دلتا باید عددی غیرصفر باشد.');
      return;
    }
    const occupied = parts.find((part) => state.partOp.has(part.id));
    if (occupied) {
      CNC.alert('warn', `برای ${CNC.partName(occupied)} ویرایش دیگری ثبت شده است؛ ابتدا آن را واگرد کنید.`);
      return;
    }
    const ids = parts.map((part) => part.id);
    state.gapOps = state.gapOps.filter((op) => op.op !== 'stretch_chain');
    state.gapOps.push({ op: 'stretch_chain', axis, parts: ids, delta });
    CNC.emit('ops-changed', {
      label: `کشش زنجیره‌ای ${parts.length} قطعه ${axis.toUpperCase()} (${CNC.sgn(delta)} mm/قطعه)`,
    });
    requestPreview();
  });
  CNC.on('selection', updateChainSelectionHint);
  updateChainSelectionHint();

  // ================================================================_منوی راست‌کلیک
  let ctxMenu = null;
  function closeCtx() { if (ctxMenu) { ctxMenu.remove(); ctxMenu = null; } }
  document.addEventListener('pointerdown', (e) => {
    if (ctxMenu && !ctxMenu.contains(e.target)) closeCtx();
  }, true);

  CNC.on('map-context', (info) => {
    closeCtx();
    if (info.dimId || info.bar || info.handle) return;
    let partId = info.partId;
    if (!partId && info.opId) {
      const op = (state.detection.operations || []).find((o) => o.id === info.opId);
      partId = op && op.assigned_part;
    }
    if (!partId) return;
    const part = findPart(partId);
    if (!part) return;
    CNC.selection.set([{ type: 'part', id: partId }]);
    const hasPartOps = state.partOp.size > 0;
    const mixed = hasPartOps || state.featureOps.length > 0;
    const contourFeature = (part.contour_feature_ids || [])[0];
    const fixed = state.fixedParts.has(partId);
    const excluded = state.outputsExcluded.has(partId);
    const item = (label, fn, opts) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.innerHTML = label;
      if (opts && opts.disabled) { b.disabled = true; if (opts.why) b.title = opts.why; }
      else b.onclick = () => { closeCtx(); fn(); };
      ctxMenu.appendChild(b);
    };
    ctxMenu = document.createElement('div');
    ctxMenu.className = 'ctxmenu';
    ctxMenu.setAttribute('role', 'menu');
    item(fixed ? '🔓 کشیدنی کن (کشش این قطعه فعال شود)' : '🔒 ثابت کن (کشش این قطعه قفل شود)',
         () => {
           if (fixed) state.fixedParts.delete(partId); else state.fixedParts.add(partId);
           CNC.emit('rerender', {}); CNC.renderInspector();
         });
    item('🚫 ' + (excluded ? 'برگرداندن به خروجی' : 'حذف از خروجی'), () => {
      if (excluded) state.outputsExcluded.delete(partId); else state.outputsExcluded.add(partId);
      CNC.applyPartStates(); CNC.renderGate(); CNC.renderInspector();
    }, { disabled: !excluded && !part.capabilities?.can_translate,
         why: 'قطعهٔ قفل را نمی‌توان علامت زد.' });
    item('⧉ تکرار آرایه…', () => {
      const count = Number(prompt('چند کپی اضافه شود؟ (۱ تا ۶۴)'));
      if (!Number.isInteger(count) || count < 1 || count > 64) return;
      const axis = prompt('محور آرایه: x یا y؟', 'x');
      if (axis !== 'x' && axis !== 'y') return;
      const pitch = Number(prompt('گام آرایه (mm — علامت‌دار):', '120'));
      if (!Number.isFinite(pitch) || Math.abs(pitch) < 1e-6) return;
      state.featureOps = state.featureOps.filter((o) => o.feature_id !== contourFeature);
      state.featureOps.push({ op: 'array', feature_id: contourFeature, axis, count, pitch });
      CNC.emit('ops-changed', { label: `آرایهٔ ${CNC.partName(part)}` });
      requestPreview();
    }, { disabled: mixed,
         why: 'با ویرایش‌های قطعه‌ای فعلی قابل ترکیب نیست؛ ویرایش‌های قطعه‌ای را واگرد کنید یا آرایه را پیش از آن‌ها بزنید.' });
    item('⇋ آینه افقی (X)', () => {
      state.featureOps = state.featureOps.filter((o) => o.feature_id !== contourFeature);
      state.featureOps.push({ op: 'mirror', feature_id: contourFeature, axis: 'x' });
      CNC.emit('ops-changed', { label: `آینهٔ ${CNC.partName(part)}` });
      requestPreview();
    }, { disabled: mixed || !part.capabilities?.can_mirror || part.locked,
         why: mixed ? 'با ویرایش‌های قطعه‌ای فعلی قابل ترکیب نیست.' : 'قطعهٔ قفل آینه نمی‌شود.' });
    item('⇋ آینه عمودی (Y)', () => {
      state.featureOps = state.featureOps.filter((o) => o.feature_id !== contourFeature);
      state.featureOps.push({ op: 'mirror', feature_id: contourFeature, axis: 'y' });
      CNC.emit('ops-changed', { label: `آینهٔ ${CNC.partName(part)}` });
      requestPreview();
    }, { disabled: mixed || !part.capabilities?.can_mirror || part.locked,
         why: mixed ? 'با ویرایش‌های قطعه‌ای فعلی قابل ترکیب نیست.' : 'قطعهٔ قفل آینه نمی‌شود.' });
    item('⟳ چرخش ۹۰° ساعت‌گرد', () => {
      state.featureOps = state.featureOps.filter((o) => o.feature_id !== contourFeature);
      state.featureOps.push({ op: 'rotate', feature_id: contourFeature, dir: 'cw' });
      CNC.emit('ops-changed', { label: `چرخش ${CNC.partName(part)}` });
      requestPreview();
    }, { disabled: mixed || !part.capabilities?.can_rotate90 || part.locked,
         why: mixed ? 'با ویرایش‌های قطعه‌ای فعلی قابل ترکیب نیست.' : 'قطعهٔ قفل نمی‌چرخد.' });
    item('⟲ چرخش ۹۰° پادساعت‌گرد', () => {
      state.featureOps = state.featureOps.filter((o) => o.feature_id !== contourFeature);
      state.featureOps.push({ op: 'rotate', feature_id: contourFeature, dir: 'ccw' });
      CNC.emit('ops-changed', { label: `چرخش ${CNC.partName(part)}` });
      requestPreview();
    }, { disabled: mixed || !part.capabilities?.can_rotate90 || part.locked,
         why: mixed ? 'با ویرایش‌های قطعه‌ای فعلی قابل ترکیب نیست.' : 'قطعهٔ قفل نمی‌چرخد.' });
    const note = document.createElement('div');
    note.className = 'ctx-note';
    note.textContent = 'آرایه/آینه/چرخش از ماشین‌آلات ویژگی‌محورِ همان موتور می‌گذرند و گتهٔ برخورد و دیالکت همیشه سرِ کار خودش است.';
    ctxMenu.appendChild(note);
    ctxMenu.style.left = Math.min(info.client.x, window.innerWidth - 240) + 'px';
    ctxMenu.style.top = Math.min(info.client.y, window.innerHeight - 340) + 'px';
    document.body.appendChild(ctxMenu);
  });

  // ================================================================_میانبرها: جهت‌نما = جابه‌جایی ۱mm (Shift=۱۰mm)
  // Enter روی قطعهٔ انتخاب‌شده = بازکردن ورودی اندازهٔ ارتفاع همان‌جا
  document.addEventListener('keydown', (e) => {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
    if (dimEditor) return;
    if (e.key === 'Enter') {
      const sel = state.selection.find((s) => s.type === 'part');
      if (!sel) return;
      const dimEl = document.querySelector(`.dim-g[data-dim="stretch:${sel.id}:y"] .dimline`);
      if (!dimEl) return;
      const r = dimEl.getBoundingClientRect();
      openDimEditor(`stretch:${sel.id}:y`, { x: r.x + r.width / 2, y: r.y + r.height / 2 });
      return;
    }
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
    if (!arrows[e.key]) return;
    const sel = state.selection.find((s) => s.type === 'part');
    if (!sel) return;
    const part = findPart(sel.id);
    if (!part || part.locked || !part.capabilities?.can_translate) return;
    if (partEditBlocked()) return;   // bugfix: مسیر کلیدترکیبی هم مثل درگ محافظت می‌شود
    e.preventDefault();
    const step = e.shiftKey ? 10 : 1;
    const [ax, ay] = arrows[e.key];
    const existing = state.partOp.get(sel.id);
    const base = existing && existing.op === 'move_part'
      ? { dx: existing.dx, dy: existing.dy } : appliedDelta(sel.id);
    const dx = Math.round((base.dx + ax * step) * 1000) / 1000;
    const dy = Math.round((base.dy + ay * step) * 1000) / 1000;
    commitOp(sel.id, { op: 'move_part', part_id: sel.id, dx, dy },
             `جابه‌جایی ${CNC.partName(part)} (${CNC.sgn(dx)},${CNC.sgn(dy)})`);
  });

  // ================================================================_اتصال به چرخهٔ رندر
  CNC.on('rendered', () => {
    drawDims();
    drawStretchBars();
    drawHandles();
  });
  CNC.on('selection', () => {
    drawStretchBars();
    drawHandles();
  });
  CNC.on('edit-cancel', () => {
    if (moveDrag || barDrag || handleDrag) {
      moveDrag = null; barDrag = null; handleDrag = null;
      clearLive(); restoreLiveStretch();
      state._gestureClaimed = false;
    }
    closeCtx();
    closeDimEditor();
  });
  CNC.on('file', () => {
    state.featureOps = [];
    closeCtx(); closeDimEditor();
    CNC.clearAlert('preview'); CNC.clearAlert('preview-api');
  });
})();
