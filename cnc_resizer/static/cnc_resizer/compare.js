/* مقایسهٔ قبل/بعد — فاز ۳.۲ (نقشه راه یکپارچه CNC/cutting)
 *
 * صادقانه: پنل «قبل» را همیشه از sourceIR (فایل اصلی بارگذاری‌شده) و «بعد» را
 * همیشه از currentIR (برنامهٔ پس از پیش‌نمایش ویرایش‌ها) می‌سازد — هیچ‌چیز از
 * layout یا حدس بازسازی نمی‌شود. هر دو pane همان viewBox اشتراکی را می‌گیرند
 * تا مقیاس یکسان باشد و مقایسهٔ چشمی معنادرست باشد (مثل مقایسهٔ ورق‌های
 * هم‌اندازه، ورق هر دو pane از sheet_bbox_finished می‌آید). رنگ هر بلوک از
 * عضویت قطعه در همان pane می‌آید (index قطعه → همان PART_COLORS نقشهٔ اصلی)،
 * پس یک قطعهٔ قبل/بعد هم‌رنگ دیده می‌شود و کپی‌های آرایه/گرید رنگ خودشان را
 * می‌گیرند. پنل فقط با پیش‌نمایش ویرایشی فعال معنا دارد؛ دکمهٔ «مقایسه»
 * در app.js فقط همان‌وقت فعال می‌شود.
 */
(function () {
  'use strict';
  const CNC = window.CNC;
  if (!CNC || !CNC.svgEl || !CNC.pathD || !CNC.buildBlockModel) return;
  const el = CNC.svgEl;

  function colorMapFrom(parts) {
    // block_id → رنگ قطعه؛ بلوک‌های بیرون از قطعه‌ها (عملیات مستقل) خنثی می‌مانند
    const m = new Map();
    (parts || []).forEach((p) => {
      const c = CNC.partColor(p);
      (p.contour_block_ids || []).forEach((id) => m.set(id, c));
      (p.children || []).forEach((ch) => (ch.block_ids || []).forEach((id) => m.set(id, c)));
    });
    return m;
  }

  function sheetRect() {
    // همان قرارداد viewport: sheet_bbox_finished حاشیهٔ ابزار دارد؛ مستطیل ورق
    // همیشه از (0,0) رسم می‌شود (فاز ۱.۱.۳) — اینجا هم همان قرارداد.
    const st = CNC.state;
    const s = (st.preview && st.preview.resized && st.preview.resized.sheet_bbox_finished)
      || (st.fileMeta && st.fileMeta.sheet_bbox_finished);
    if (!s) return null;
    const w = s.width != null ? s.width : (s.x_max - s.x_min);
    const h = s.height != null ? s.height : (s.y_max - s.y_min);
    return (w > 0 && h > 0) ? { w, h } : null;
  }

  function unionBB(models, sheet) {
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    const eat = (a, b, c, d) => {
      x1 = Math.min(x1, a); y1 = Math.min(y1, b);
      x2 = Math.max(x2, c); y2 = Math.max(y2, d);
    };
    if (sheet) eat(0, 0, sheet.w, sheet.h);
    for (const m of models) {
      if (!m) continue;
      for (const blk of m.blocks.values()) {
        const r = blk.bbox;
        if (r) eat(r.x_min, r.y_min, r.x_max, r.y_max);
      }
    }
    return (x1 === Infinity) ? null : { x1, y1, x2, y2 };
  }

  function drawPane(svg, model, colors, sheet) {
    svg.textContent = '';
    if (sheet) {
      el('rect', { x: 0, y: -sheet.h, width: sheet.w, height: sheet.h, class: 'cmp-sheet' }, svg);
    }
    if (!model) return;
    for (const blk of model.blocks.values()) {
      el('path', {
        d: CNC.pathD(blk.segments), class: 'cmp-blk',
        stroke: colors.get(blk.id) || 'var(--ghost)', fill: 'none',
      }, svg);
    }
  }

  CNC.renderCompare = function renderCompare() {
    const panel = document.getElementById('comparePanel');
    if (!panel || panel.hidden) return;
    const st = CNC.state;
    const beforeModel = st.sourceIR ? CNC.buildBlockModel(st.sourceIR) : null;
    const afterIR = st.currentIR || st.sourceIR;
    const afterModel = afterIR ? CNC.buildBlockModel(afterIR) : null;
    // رنگ‌بندی از detection (عضویت بلوک ← قطعه) — همان فلسفهٔ viewport:
    // بلوک‌ها بین قبل/بعد پایدارند (وصلهٔ 1:1) پس عضویت‌شان هویت پایدار قطعه
    // است (بعدِ preview رندر afterPairs را با hit||p به همان قبل برمی‌گرداند).
    // کپی‌های آرایه/گرید با پل _copyMap رنگ مبدأ را می‌گیرند — همان قاعدهٔ
    // viewport.js برای رنگ کپی‌ها.
    const baseColors = colorMapFrom(st.detection && st.detection.parts);
    const beforeColors = baseColors;
    const afterColors = new Map(baseColors);
    if (st._copyMap) {
      for (const [newId, srcId] of st._copyMap) {
        if (!afterColors.has(newId) && afterColors.has(srcId)) {
          afterColors.set(newId, afterColors.get(srcId));
        }
      }
    }
    const sheet = sheetRect();
    const stats = document.getElementById('cmpStats');
    const svgs = [document.getElementById('cmpBefore'), document.getElementById('cmpAfter')];
    const bb = unionBB([beforeModel, afterModel], sheet);
    if (!bb) {
      svgs.forEach((s) => { if (s) s.textContent = ''; });
      if (stats) stats.textContent = 'چیزی برای مقایسه نیست — اول یک فایل باز کنید.';
      return;
    }
    // viewBox اشتراکی (قرارداد viewport: محور y معکوس — pathD خودش -y می‌نویسد)
    const pad = 0.03 * Math.max(bb.x2 - bb.x1, bb.y2 - bb.y1, 1);
    const vbStr = `${bb.x1 - pad} ${-(bb.y2 + pad)} ${(bb.x2 - bb.x1) + 2 * pad} ${(bb.y2 - bb.y1) + 2 * pad}`;
    svgs.forEach((s) => { if (s) s.setAttribute('viewBox', vbStr); });
    drawPane(svgs[0], beforeModel, beforeColors, sheet);
    drawPane(svgs[1], afterModel, afterColors, sheet);
    if (stats) {
      const parts = ((st.detection && st.detection.parts) || []).length;
      const bBlocks = beforeModel ? beforeModel.blocks.size : 0;
      const aBlocks = afterModel ? afterModel.blocks.size : 0;
      const copies = st._copyMap ? st._copyMap.size : 0;
      let line = `قطعه‌ها: ${parts} (عضویت بلوک‌ها بین قبل/بعد پایدار است)`
        + ` · بلوک‌های برش — قبل: ${bBlocks} / بعد: ${aBlocks}`;
      if (copies > 0) line += ` · ${copies} بلوک کپیٔ تازه (آرایه/گرید)`;
      if (!st.preview) line += ' — پیش‌نمایش فعالی نیست؛ «بعد» همان فایل اصلی است.';
      stats.textContent = line;
    }
  };
})();
