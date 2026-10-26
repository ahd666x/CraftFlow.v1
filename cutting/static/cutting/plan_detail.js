/* برنامهٔ برش v2 — ریل کناری ورق‌ها + نقشهٔ بزرگ + نوار نتیجهٔ ثابت.
 * منطق اعتبارسنجی پرت، خلاصه، «همه مصرف شدند» و مودال تأیید دست‌نخورده مانده است.
 * جابجایی ورق‌ها: کلیک روی دایره‌های ریل یا کلیدهای ← → (بدون نوار قبلی/بعدی).
 */
(() => {
  const MAX_ROWS = 50;
  const panels = [...document.querySelectorAll('[data-sheet-panel]')];
  const dots = [...document.querySelectorAll('.rail-dot')];
  const railMats = [...document.querySelectorAll('.rail-mat')];
  const sections = [...document.querySelectorAll('[data-material-section]')];
  const groups = [...document.querySelectorAll('.outcome-group')];
  const remnantContainers = [...document.querySelectorAll('.remnant-fields')];
  const remPop = document.getElementById('remnants-pop');
  const remToggle = document.querySelector('[data-dock-toggle="result"]');
  const sheetsPop = document.getElementById('sheets-pop');
  const sheetsToggle = document.querySelector('[data-dock-toggle="sheets"]');
  const number = value => Number(String(value || '').replace(',', '.')) || 0;
  const fmt = value => Number(value || 0).toLocaleString('en-US');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
  const outcomeOf = id => document.querySelector(`input[name="outcome_${id}"]:checked`)?.value || '';

  function storedOutcome(panel) {
    const value = outcomeOf(panel.dataset.sheetPk) || panel.dataset.outcome || 'none';
    return ['used', 'untouched', 'scrapped'].includes(value) ? value : 'none';
  }

  /* ---------- rail dots ---------- */

  function updateDot(sheetId) {
    const panel = panels.find(item => item.dataset.sheetPk === String(sheetId));
    const value = panel ? storedOutcome(panel) : 'none';
    dots.filter(dot => dot.dataset.sheetPk === String(sheetId)).forEach(dot => {
      dot.classList.remove('outcome-none', 'outcome-reserved', 'outcome-used', 'outcome-untouched', 'outcome-scrapped');
      dot.classList.add(`outcome-${value}`);
      const labels = { none: 'ثبت‌نشده', used: 'مصرف‌شده', untouched: 'دست‌نخورده', scrapped: 'ضایعات' };
      dot.title = `ورق ${dot.textContent.trim()} — ${labels[value] || 'ثبت‌نشده'}`;
    });
  }

  function updateRailGroups() {
    sections.forEach(section => {
      const own = panels.filter(panel => section.contains(panel));
      const done = own.filter(panel => storedOutcome(panel) !== 'none').length;
      const key = section.dataset.materialSection;
      const mat = railMats.find(item => item.dataset.materialTab === key);
      if (mat) {
        mat.classList.toggle('is-ok', own.length > 0 && done === own.length);
        mat.title = `${mat.getAttribute('aria-label') || ''} — ${done} از ${own.length} ثبت شده`;
      }
    });
  }

  function updateProgress() {
    const counts = { reserved: 0, used: 0, untouched: 0, scrapped: 0 };
    panels.forEach(panel => { counts[storedOutcome(panel)] = (counts[storedOutcome(panel)] || 0) + 1; });
    const total = panels.length;
    const completed = counts.used + counts.untouched + counts.scrapped;
    const percent = total ? Math.round(completed * 100 / total) : 0;
    const bar = document.getElementById('sheet-progress-bar');
    const label = document.getElementById('sheet-progress-label');
    if (bar) bar.style.width = `${percent}%`;
    if (label) label.textContent = `${completed} از ${total} ورق ثبت شده`;
    document.querySelectorAll('[data-count]').forEach(node => {
      node.textContent = counts[node.dataset.count] || 0;
    });
    const progress = bar?.closest('[role="progressbar"]');
    if (progress) progress.setAttribute('aria-valuenow', String(percent));
    updateRailGroups();
  }

  /* ---------- sheet switching ---------- */

  let current = null;

  function showPanel(panel) {
    if (!panel || !panels.length) return;
    current = panel;
    const section = panel.closest('[data-material-section]');
    sections.forEach(item => item.classList.toggle('d-none', item !== section));
    panels.forEach(item => item.classList.toggle('d-none', item !== panel));
    dots.forEach(dot => {
      const active = dot.dataset.sheetPk === panel.dataset.sheetPk;
      dot.classList.toggle('active', active);
      dot.setAttribute('aria-current', active ? 'true' : 'false');
    });
    railMats.forEach(mat => {
      const active = Boolean(section) && mat.dataset.materialTab === section.dataset.materialSection;
      mat.setAttribute('aria-current', active ? 'true' : 'false');
    });
    // نوار نتیجه و پنل پرت فقط گروه ورق جاری را نشان می‌دهند
    const pk = panel.dataset.sheetPk;
    groups.forEach(group => group.classList.toggle('d-none', group.dataset.barGroup !== pk));
    remnantContainers.forEach(container => container.classList.toggle('d-none', container.dataset.barGroup !== pk));
    // هیچ لایه‌ای باز نمی‌ماند
    document.querySelectorAll('[data-verlist-dock]').forEach(dock => { dock.hidden = true; });
    document.querySelectorAll('[data-dock-toggle="verlist"]').forEach(btn => {
      btn.classList.remove('active');
      btn.setAttribute('aria-expanded', 'false');
    });
    // بازبینی UI/UX بند ۸: ردیف جاری در نمای خلاصه هم علامت‌گذاری شود
    document.querySelectorAll('[data-summary-row]').forEach(row => {
      row.classList.toggle('active', row.dataset.sheetJump === pk);
    });
  }

  function stepPanel(dir) {
    if (!current) return;
    const index = panels.indexOf(current) + dir;
    if (index < 0 || index >= panels.length) return;
    showPanel(panels[index]);
  }

  /* ---------- بازبینی UI/UX بند ۲۵: زوم/پن نقشهٔ برش ----------
   * هر ورق حالت زوم خودش را دارد (state جدا برای هر .map-stage). دکمه‌های
   * +/−/نمایش کامل، اسکرول به‌سوی مکان‌نما، درگ برای پن و پینچ دو-انگشتی.
   * تا وقتی زوم ۱ است لمس عمودی مثل قبل صفحه را اسکرول می‌کند؛ فقط در
   * حالت زوم‌شده کشیدن روی نقشه پن می‌شود (touch-action مشروط). */
  const zoomAppliers = [];
  document.querySelectorAll('.map-stage').forEach((stage) => {
    const fit = stage.querySelector('.map-fit');
    const svg = stage.querySelector('.cut-map');
    if (!fit || !svg) return;
    const badge = stage.querySelector('[data-map-badge]');
    const st = { k: 1, tx: 0, ty: 0 };

    const apply = () => {
      st.k = Math.min(10, Math.max(1, st.k));
      const w = fit.clientWidth, h = fit.clientHeight;
      st.tx = Math.min(0, Math.max(w * (1 - st.k), st.tx));
      st.ty = Math.min(0, Math.max(h * (1 - st.k), st.ty));
      if (st.k === 1) { st.tx = 0; st.ty = 0; }
      svg.style.transform = `translate(${st.tx}px, ${st.ty}px) scale(${st.k})`;
      stage.classList.toggle('is-zoomed', st.k > 1);
      if (badge) {
        badge.hidden = st.k === 1;
        badge.textContent = `${Math.round(st.k * 100)}%`;
      }
    };
    const zoomAt = (factor, cx, cy) => {   // cx/cy نسبت به .map-fit
      const k0 = st.k;
      const k1 = Math.min(10, Math.max(1, k0 * factor));
      if (k1 === k0) return;
      st.tx = cx - ((cx - st.tx) * k1) / k0;
      st.ty = cy - ((cy - st.ty) * k1) / k0;
      st.k = k1;
      apply();
    };
    const localPoint = (clientX, clientY) => {
      const r = fit.getBoundingClientRect();
      return { x: clientX - r.left, y: clientY - r.top };
    };

    stage.querySelector('[data-map-zin]')?.addEventListener('click', () => {
      zoomAt(1.35, fit.clientWidth / 2, fit.clientHeight / 2);
    });
    stage.querySelector('[data-map-zout]')?.addEventListener('click', () => {
      zoomAt(1 / 1.35, fit.clientWidth / 2, fit.clientHeight / 2);
    });
    stage.querySelector('[data-map-fit]')?.addEventListener('click', () => { st.k = 1; apply(); });
    badge?.addEventListener('click', () => { st.k = 1; apply(); });

    fit.addEventListener('wheel', (e) => {
      e.preventDefault();
      const p = localPoint(e.clientX, e.clientY);
      zoomAt(Math.exp(-e.deltaY * 0.0015), p.x, p.y);
    }, { passive: false });

    // درگ پن + پینچ دو-انگشتی با pointer events
    const pointers = new Map();
    let pinchDist = 0, last = null;
    fit.addEventListener('pointerdown', (e) => {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinchDist = Math.hypot(a.x - b.x, a.y - b.y);
        last = null;
      } else if (pointers.size === 1) {
        last = { x: e.clientX, y: e.clientY };
        try { fit.setPointerCapture(e.pointerId); } catch (_) { /* pointer نامعتبر */ }
      }
    });
    fit.addEventListener('pointermove', (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2 && pinchDist > 0) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (Math.abs(d - pinchDist) > 1) {
          const mid = localPoint((a.x + b.x) / 2, (a.y + b.y) / 2);
          zoomAt(pinchDist / d, mid.x, mid.y);
          pinchDist = d;
        }
        return;
      }
      if (pointers.size === 1 && st.k > 1 && last) {
        st.tx += e.clientX - last.x;
        st.ty += e.clientY - last.y;
        last = { x: e.clientX, y: e.clientY };
        stage.classList.add('is-panning');
        apply();
      }
    });
    const endPointer = (e) => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinchDist = 0;
      if (!pointers.size) { last = null; stage.classList.remove('is-panning'); }
    };
    fit.addEventListener('pointerup', endPointer);
    fit.addEventListener('pointercancel', endPointer);
    zoomAppliers.push(apply);
    apply();
  });
  window.addEventListener('resize', () => zoomAppliers.forEach((fn) => fn()));

  /* ---------- remnants ---------- */

  function validateRemnants(container) {
    let area = 0;
    let invalid = false;
    const outcome = outcomeOf(container.dataset.sheet);
    const maxWidth = number(container.dataset.width);
    const maxHeight = number(container.dataset.height);
    if (outcome !== 'used') {
      container.querySelectorAll('[data-dimension]').forEach(input => input.classList.remove('is-invalid'));
      container.querySelector('.remnant-area-total').textContent = '0 m²';
      container.querySelector('.remnant-validation').textContent = '';
      return true;
    }
    container.querySelectorAll('[data-remnant-row]').forEach(row => {
      const widthInput = row.querySelector('[data-dimension="width"]');
      const heightInput = row.querySelector('[data-dimension="height"]');
      const width = number(widthInput.value);
      const height = number(heightInput.value);
      widthInput.classList.remove('is-invalid');
      heightInput.classList.remove('is-invalid');
      if (!widthInput.value && !heightInput.value) return;
      if (!widthInput.value || !heightInput.value || width > maxWidth || height > maxHeight
        || Math.round(width * 10) !== width * 10 || Math.round(height * 10) !== height * 10) {
        invalid = true;
        if (!widthInput.value || width > maxWidth || !width || Math.round(width * 10) !== width * 10) widthInput.classList.add('is-invalid');
        if (!heightInput.value || height > maxHeight || !height || Math.round(height * 10) !== height * 10) heightInput.classList.add('is-invalid');
        return;
      }
      area += width * height;
    });
    if (area > maxWidth * maxHeight) {
      invalid = true;
      container.querySelectorAll('[data-dimension]').forEach(input => {
        if (input.value) input.classList.add('is-invalid');
      });
    }
    container.querySelector('.remnant-area-total').textContent = `${fmt(area / 1000000)} m²`;
    container.querySelector('.remnant-validation').textContent = invalid
      ? 'ابعاد پرت یا مجموع مساحت با ورق مبدأ سازگار نیست؛ هر مقدار باید حداکثر یک رقم اعشار داشته باشد.'
      : '';
    return !invalid;
  }

  function renumber(container) {
    const sheet = container.dataset.sheet;
    const rows = [...container.querySelectorAll('[data-remnant-row]')];
    const outcome = outcomeOf(sheet);
    rows.forEach((row, index) => {
      row.querySelector('.remnant-number').textContent = `پرت ${index + 1}`;
      row.querySelectorAll('[data-dimension]').forEach(input => {
        input.name = `rem_${sheet}_${index + 1}_${input.dataset.dimension}`;
      });
      row.querySelector('.remove-remnant').disabled = rows.length === 1 || outcome !== 'used';
    });
    container.querySelector('.add-remnant').disabled = rows.length >= MAX_ROWS || outcome !== 'used';
    container.querySelector('.fill-predicted-offcuts').disabled = outcome !== 'used';
    validateRemnants(container);
  }

  /* ---------- summary ---------- */

  function updateSummary() {
    const chosen = groups.filter(group => outcomeOf(group.dataset.sheet));
    const used = groups.filter(group => outcomeOf(group.dataset.sheet) === 'used').length;
    const scrapped = groups.filter(group => outcomeOf(group.dataset.sheet) === 'scrapped').length;
    const untouched = groups.filter(group => outcomeOf(group.dataset.sheet) === 'untouched').length;
    const offcuts = remnantContainers.reduce((sum, container) => {
      if (outcomeOf(container.dataset.sheet) !== 'used') return sum;
      return sum + [...container.querySelectorAll('[data-remnant-row]')]
        .filter(row => [...row.querySelectorAll('[data-dimension]')].some(input => input.value)).length;
    }, 0);
    const pieces = panels.filter(panel => outcomeOf(panel.dataset.sheetPk) === 'used')
      .reduce((sum, panel) => sum + number(panel.dataset.pieceCount), 0);
    const summary = `${fmt(used + scrapped)} ورق از انبار کسر می‌شود (${fmt(used)} مصرف‌شده، ${fmt(scrapped)} ضایعات) · ${fmt(untouched)} ورق دست‌نخورده برمی‌گردد · ${fmt(offcuts)} پرت واقعی افزوده می‌شود · ${fmt(pieces)} قطعه تکمیل می‌شود`;
    const result = document.getElementById('result-summary');
    if (result) {
      result.textContent = chosen.length === groups.length
        ? summary
        : `نتیجه ${fmt(chosen.length)} از ${fmt(groups.length)} ورق انتخاب شده است. ${summary}`;
    }
    return { chosen: chosen.length, total: groups.length, used, scrapped, untouched, offcuts, pieces, summary };
  }

  function syncOutcome(sheet) {
    const outcome = outcomeOf(sheet);
    const panel = panels.find(item => item.dataset.sheetPk === sheet);
    if (panel) panel.querySelectorAll('.cut-piece').forEach(piece => {
      piece.classList.remove('used', 'scrapped');
      if (outcome === 'used') piece.classList.add('used');
      if (outcome === 'scrapped') piece.classList.add('scrapped');
    });
    const container = document.getElementById(`remnants-${sheet}`);
    const note = document.getElementById(`note-${sheet}`);
    if (note) note.required = outcome === 'scrapped';
    if (container) {
      container.querySelectorAll('input[data-dimension]').forEach(input => { input.disabled = outcome !== 'used'; });
      renumber(container);
    }
    updateDot(sheet);
    updateProgress();
    updateSummary();
    syncSheetsSummary();
  }

  /* ---------- validation focus ---------- */

  function setRemPanel(open) {
    if (!remPop || !remToggle) return;
    remPop.hidden = !open;
    remToggle.setAttribute('aria-expanded', String(open));
    if (open) {
      const visible = remPop.querySelector('.remnant-fields:not(.d-none)');
      // بازبینی UI/UX بند ۷: برای «ضایعات» فوکوس روی یادداشتِ الزامی، وگرنه اولین پرت
      const target = outcomeOf(visible?.dataset.sheet) === 'scrapped'
        ? visible?.querySelector('input[name^="note_"]')
        : visible?.querySelector('[data-dimension="width"]');
      target?.focus();
    }
  }

  /* ---------- خلاصهٔ همهٔ ورق‌ها (بازبینی UI/UX بند ۸) ---------- */

  const OUTCOME_LABELS = { none: 'ثبت‌نشده', used: 'مصرف', untouched: 'دست‌نخورده', scrapped: 'ضایعات' };
  const OUTCOME_BADGE = { used: 'is-ok', untouched: 'is-mute', scrapped: 'is-bad', none: '' };

  function remnantCountOf(pk) {
    const container = document.getElementById(`remnants-${pk}`);
    if (!container) return 0;
    return [...container.querySelectorAll('[data-remnant-row]')]
      .filter(row => [...row.querySelectorAll('[data-dimension]')].some(input => input.value)).length;
  }

  function syncSheetsSummary() {
    if (!sheetsPop) return;
    document.querySelectorAll('[data-summary-row]').forEach(row => {
      const pk = row.dataset.sheetJump;
      const value = storedOutcome(panels.find(panel => panel.dataset.sheetPk === String(pk)) || {});
      const badge = row.querySelector('[data-summary-outcome]');
      if (badge) {
        badge.textContent = OUTCOME_LABELS[value] || 'ثبت‌نشده';
        badge.className = `badge-status ${OUTCOME_BADGE[value] || ''}`;
      }
      const note = row.querySelector('[data-summary-note]');
      if (note) {
        if (value === 'used') {
          const count = remnantCountOf(pk);
          note.textContent = count ? `${fmt(count)} پرت واقعی ثبت شده` : 'پرت واقعی ثبت نشده';
        } else if (value === 'scrapped') {
          const noteInput = document.getElementById(`note-${pk}`);
          note.textContent = noteInput && noteInput.value.trim() ? '✓ یادداشت ثبت شده' : 'یادداشت الزامی است';
        } else {
          note.textContent = '';
        }
      }
    });
  }

  function setSheetsPop(open) {
    if (!sheetsPop || !sheetsToggle) return;
    if (open && remPop && !remPop.hidden) setRemPanel(false); // دو پنل هم‌مکان‌اند
    sheetsPop.hidden = !open;
    sheetsToggle.setAttribute('aria-expanded', String(open));
    if (open) syncSheetsSummary();
  }

  function markInvalidPanel(field) {
    const container = field.closest('.remnant-fields');
    if (container) {
      const panel = panels.find(item => item.dataset.sheetPk === container.dataset.sheet);
      showPanel(panel);
      setRemPanel(true);
    } else {
      showPanel(field.closest('[data-sheet-panel]'));
    }
    field.focus();
  }

  function allInputsValid() {
    let valid = true;
    let focusTarget = null;
    for (const group of groups) {
      const sheet = group.dataset.sheet;
      const outcome = outcomeOf(sheet);
      if (!outcome) {
        const panel = panels.find(item => item.dataset.sheetPk === sheet);
        showPanel(panel);
        panel?.querySelector('input.cut-outcome')?.closest('.out-opt')?.focus();
        return false;
      }
      const note = document.getElementById(`note-${sheet}`);
      if (outcome === 'scrapped' && !note.value.trim()) {
        valid = false;
        focusTarget = note;
        break;
      }
      const container = document.getElementById(`remnants-${sheet}`);
      if (container && !validateRemnants(container)) {
        valid = false;
        focusTarget = [...container.querySelectorAll('[data-dimension]')].find(input => input.classList.contains('is-invalid'));
        break;
      }
      if (container) {
        const bad = [...container.querySelectorAll('[data-dimension]')].find(input => input.value && !input.checkValidity());
        if (bad) {
          valid = false;
          focusTarget = bad;
          break;
        }
      }
    }
    if (!valid && focusTarget) markInvalidPanel(focusTarget);
    return valid;
  }

  function renderSummaryTable(summary) {
    const rows = [
      ['مصرف‌شده', summary.used],
      ['ضایعات', summary.scrapped],
      ['برگشت به انبار', summary.untouched],
      ['پرت جدید', summary.offcuts],
      ['قطعه تکمیل‌شده', summary.pieces]
    ];
    return `<table class="table table-sm mb-0"><tbody>${rows.map(([label, value]) => `<tr><th scope="row">${label}</th><td class="text-end">${fmt(value)}</td></tr>`).join('')}</tbody></table>`;
  }

  /* بازبینی UI/UX بند ۱۴: مودال تأیید باید نام/شمارهٔ ورق‌ها را هم نشان دهد */
  function renderSheetLines() {
    const lines = groups.map(group => {
      const pk = group.dataset.sheet;
      const value = outcomeOf(pk) || 'none';
      const dot = dots.find(item => item.dataset.sheetPk === String(pk));
      const no = dot ? dot.textContent.trim() : '?';
      const panel = panels.find(item => item.dataset.sheetPk === String(pk));
      const pieces = panel ? number(panel.dataset.pieceCount) : 0;
      let extra = '';
      if (value === 'used') {
        const count = remnantCountOf(pk);
        if (count) extra = ` — ${fmt(count)} پرت واقعی`;
      } else if (value === 'scrapped') {
        const noteInput = document.getElementById(`note-${pk}`);
        const text = noteInput?.value.trim();
        if (text) extra = ` — یادداشت: ${esc(text)}`;
      }
      return `<li>ورق <b class="num">${esc(no)}</b> — ${OUTCOME_LABELS[value] || 'ثبت‌نشده'} <span class="text-muted">· ${fmt(pieces)} قطعه</span>${extra}</li>`;
    }).join('');
    return `<ul class="small mt-2 mb-0 ps-3">${lines}</ul>`;
  }

  /* ---------- events ---------- */

  remnantContainers.forEach(container => renumber(container));
  panels.forEach(panel => updateDot(panel.dataset.sheetPk));
  panels.forEach(panel => {
    const dot = dots.find(item => item.dataset.sheetPk === panel.dataset.sheetPk);
    container_sync_no(panel, dot);
  });
  function container_sync_no(panel, dot) {
    const container = document.getElementById(`remnants-${panel.dataset.sheetPk}`);
    if (container && dot) container.querySelector('.remnant-sheet-no').textContent = dot.textContent.trim();
  }
  updateProgress();

  document.querySelectorAll('input.cut-outcome').forEach(radio => {
    radio.addEventListener('change', () => {
      syncOutcome(radio.dataset.sheet);
      // fallback مرورگرهای بدون :has() — انتخابِ نتیجه باید بدون CSS4 هم دیده شود (بازبینی UI/UX بند ۱۷)
      radio.closest('.outcome-radios')?.querySelectorAll('.out-opt').forEach(label => {
        label.classList.toggle('is-checked', label.contains(radio) && radio.checked);
      });
      // بازبینی UI/UX بند ۷: یادداشتِ الزامی «ضایعات» باید همان‌جا که نتیجه انتخاب می‌شود
      // باز و فوکوس شود — نه فقط بعد از خطای اعتبارسنجی.
      if (radio.checked && radio.value === 'scrapped') setRemPanel(true);
    });
  });
  dots.forEach(dot => dot.addEventListener('click', () => {
    showPanel(panels.find(item => item.dataset.sheetPk === dot.dataset.sheetPk));
  }));
  railMats.forEach(mat => mat.addEventListener('click', () => {
    const section = sections.find(item => item.dataset.materialSection === mat.dataset.materialTab);
    const own = panels.filter(panel => section && section.contains(panel));
    showPanel(own.find(panel => storedOutcome(panel) === 'none') || own[0]);
  }));
  document.addEventListener('keydown', event => {
    if (event.target.matches('input, select, textarea')) return;
    if (event.key === 'ArrowLeft') stepPanel(1);
    if (event.key === 'ArrowRight') stepPanel(-1);
  });

  document.querySelectorAll('[data-dock-toggle]').forEach(btn => btn.addEventListener('click', () => {
    if (btn.dataset.dockToggle === 'result') { setRemPanel(remPop?.hidden ?? true); return; }
    if (btn.dataset.dockToggle === 'sheets') { setSheetsPop(sheetsPop?.hidden ?? true); return; }
    const dock = document.getElementById(btn.getAttribute('aria-controls') || '');
    if (!dock) return;
    const open = dock.hidden;
    document.querySelectorAll('[data-verlist-dock]').forEach(node => { node.hidden = true; });
    document.querySelectorAll('[data-dock-toggle="verlist"]').forEach(node => {
      node.setAttribute('aria-expanded', 'false');
      node.classList.remove('active');
    });
    dock.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    btn.classList.toggle('active', open);
  }));
  document.querySelector('[data-rem-close]')?.addEventListener('click', () => setRemPanel(false));
  document.querySelector('[data-sheets-close]')?.addEventListener('click', () => setSheetsPop(false));
  document.querySelectorAll('[data-sheet-jump]').forEach(row => row.addEventListener('click', () => {
    showPanel(panels.find(item => item.dataset.sheetPk === row.dataset.sheetJump));
    setSheetsPop(false);
  }));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      if (remPop && !remPop.hidden) setRemPanel(false);
      if (sheetsPop && !sheetsPop.hidden) setSheetsPop(false);
    }
  });

  document.querySelectorAll('.add-remnant').forEach(button => button.addEventListener('click', () => {
    const container = document.getElementById(button.dataset.target);
    const rows = container.querySelector('.remnant-rows');
    if (rows.children.length >= MAX_ROWS) return;
    const clone = rows.firstElementChild.cloneNode(true);
    clone.querySelectorAll('input').forEach(input => {
      input.value = '';
      input.classList.remove('is-invalid');
    });
    rows.appendChild(clone);
    renumber(container);
    clone.querySelector('[data-dimension="width"]').focus();
  }));

  document.querySelectorAll('.fill-predicted-offcuts').forEach(button => button.addEventListener('click', () => {
    const container = document.getElementById(button.dataset.target);
    if (outcomeOf(container.dataset.sheet) !== 'used') return;
    let predictions = [];
    try {
      predictions = JSON.parse(container.dataset.expected || '[]')
        .filter(item => item.width && item.height).slice(0, MAX_ROWS);
    } catch (_) {
      predictions = [];
    }
    if (!predictions.length) {
      container.querySelector('.remnant-validation').textContent = 'پرت تخمینی قابل استفاده‌ای برای این ورق ثبت نشده است.';
      return;
    }
    const rows = container.querySelector('.remnant-rows');
    while (rows.children.length < predictions.length) {
      rows.appendChild(rows.firstElementChild.cloneNode(true));
    }
    while (rows.children.length > predictions.length && rows.children.length > 1) {
      rows.lastElementChild.remove();
    }
    [...rows.querySelectorAll('[data-remnant-row]')].forEach((row, index) => {
      row.querySelector('[data-dimension="width"]').value = String(predictions[index].width).replace(',', '.');
      row.querySelector('[data-dimension="height"]').value = String(predictions[index].height).replace(',', '.');
      row.querySelectorAll('[data-dimension]').forEach(input => input.classList.remove('is-invalid'));
    });
    renumber(container);
    container.querySelector('.remnant-validation').textContent = 'مقادیر تخمینی وارد شد؛ پیش از ثبت، اندازه‌ها را بررسی و اندازه‌گیری کنید.';
    updateSummary();
  }));

  document.addEventListener('click', event => {
    if (!event.target.classList.contains('remove-remnant')) return;
    const container = event.target.closest('.remnant-fields');
    const rows = container.querySelector('.remnant-rows');
    if (rows.children.length > 1) {
      event.target.closest('[data-remnant-row]').remove();
      renumber(container);
      updateSummary();
    }
  });
  document.addEventListener('input', event => {
    if (!event.target.matches('.remnant-fields [data-dimension]')) return;
    const container = event.target.closest('.remnant-fields');
    validateRemnants(container);
    updateSummary();
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || !event.target.matches('.remnant-fields [data-dimension="height"]')) return;
    event.preventDefault();
    const button = event.target.closest('.remnant-fields').querySelector('.add-remnant');
    if (!button.disabled) button.click();
  });

  document.getElementById('mark-all-used')?.addEventListener('click', () => {
    document.querySelectorAll('input.cut-outcome[value="used"]').forEach(radio => {
      radio.checked = true;
      radio.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });
  document.getElementById('open-confirm')?.addEventListener('click', () => {
    if (!allInputsValid()) return;
    const summary = updateSummary();
    document.getElementById('confirm-cut-summary').innerHTML = renderSummaryTable(summary) + renderSheetLines();
    bootstrap.Modal.getOrCreateInstance(document.getElementById('confirm-cut-modal')).show();
  });
  document.getElementById('cut-results-form')?.addEventListener('submit', event => {
    if (!allInputsValid()) {
      event.preventDefault();
      return;
    }
    const button = document.getElementById('confirm-cut-submit');
    button.disabled = true;
    button.querySelector('.spinner-border').hidden = false;
    button.querySelector('span:first-child').textContent = 'در حال ثبت…';
  });

  document.getElementById('print-operator-sheet')?.addEventListener('click', () => window.print());
  // تأیید لغو برنامه → مودال درون‌برنامه‌ای (SelviDialog) با رهگیری سراسری
  // data-confirm در ui-dialogs.js — listener محلی confirm حذف شد (بند ب-۸).

  const downloadTrigger = document.getElementById('cut-download-trigger');
  const warningElement = document.getElementById('cut-warning-modal');
  if (downloadTrigger && warningElement) {
    const warningModal = bootstrap.Modal.getOrCreateInstance(warningElement);
    const acknowledgement = document.getElementById('cut-warning-ack');
    const downloadButton = document.getElementById('cut-warning-download');
    const storageKey = 'craftflow-cut-file-warning-accepted-v1';
    const isAccepted = () => {
      try { return localStorage.getItem(storageKey) === '1'; } catch (_) { return false; }
    };
    downloadTrigger.addEventListener('click', event => {
      if (isAccepted()) return;
      event.preventDefault();
      warningModal.show();
    });
    acknowledgement.addEventListener('change', () => { downloadButton.disabled = !acknowledgement.checked; });
    downloadButton.addEventListener('click', () => {
      if (!acknowledgement.checked) return;
      try { localStorage.setItem(storageKey, '1'); } catch (_) { /* Keep this download usable when storage is blocked. */ }
      warningModal.hide();
      window.location.assign(downloadTrigger.href);
    });
  }

  const firstUnresolved = panels.find(panel => !outcomeOf(panel.dataset.sheetPk));
  showPanel(firstUnresolved || panels[0]);
  updateSummary();
})();
