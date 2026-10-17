(() => {
  const MAX_ROWS = 50;
  const panels = [...document.querySelectorAll('[data-sheet-panel]')];
  const tabs = [...document.querySelectorAll('.sheet-tab')];
  const groups = [...document.querySelectorAll('.outcome-group')];
  const number = value => Number(String(value || '').replace(',', '.')) || 0;
  const fmt = value => Number(value || 0).toLocaleString('en-US');
  const outcomeOf = id => document.querySelector(`input[name="outcome_${id}"]:checked`)?.value || '';

  function storedOutcome(panel) {
    const value = outcomeOf(panel.dataset.sheetPk) || panel.dataset.outcome || 'none';
    return ['used', 'untouched', 'scrapped'].includes(value) ? value : 'none';
  }

  function updateTabStatus(sheetId) {
    const panel = panels.find(item => item.dataset.sheetPk === String(sheetId));
    const value = panel ? storedOutcome(panel) : 'none';
    tabs.filter(tab => tab.dataset.sheetPk === String(sheetId)).forEach(tab => {
      tab.classList.remove('outcome-none', 'outcome-used', 'outcome-untouched', 'outcome-scrapped');
      tab.classList.add(`outcome-${value}`);
      const status = tab.querySelector('.sheet-tab-status');
      if (status) status.textContent = ({
        none: 'ثبت‌نشده', used: 'مصرف‌شده', untouched: 'دست‌نخورده', scrapped: 'اسقاط'
      })[value] || 'ثبت‌نشده';
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
    if (bar) { bar.style.width = `${percent}%`; bar.textContent = `${percent}%`; }
    if (label) label.textContent = `${completed} از ${total} ورق ثبت شده`;
    document.querySelectorAll('[data-count]').forEach(node => { node.textContent = counts[node.dataset.count] || 0; });
    const progress = bar?.closest('[role="progressbar"]');
    if (progress) progress.setAttribute('aria-valuenow', String(percent));
  }

  function showPanel(panel) {
    if (!panel || !panels.length) return;
    panels.forEach(item => item.classList.toggle('d-none', item !== panel));
    tabs.forEach(tab => {
      const active = tab.dataset.sheetPk === panel.dataset.sheetPk;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    const index = panels.indexOf(panel);
    const previous = panel.querySelector('.previous-sheet');
    const next = panel.querySelector('.next-sheet');
    const position = panel.querySelector('.sheet-position');
    if (previous) previous.disabled = index <= 0;
    if (next) {
      next.disabled = index >= panels.length - 1;
      next.textContent = `ورق ${index + 1} از ${panels.length} — بعدی ←`;
    }
    if (position) position.textContent = `ورق ${index + 1} از ${panels.length}`;
  }

  function validateRemnants(container) {
    let area = 0;
    let invalid = false;
    const outcome = outcomeOf(container.dataset.sheet);
    const maxWidth = number(container.dataset.width);
    const maxHeight = number(container.dataset.height);
    if (outcome !== 'used') {
      container.querySelectorAll('[data-dimension]').forEach(input => input.classList.remove('is-invalid'));
      container.querySelector('.remnant-area-total').textContent = 'مساحت ثبت‌شده: 0 m²';
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
    container.querySelector('.remnant-area-total').textContent = `مساحت ثبت‌شده: ${fmt(area / 1000000)} m²`;
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

  function updateSummary() {
    const chosen = groups.filter(group => outcomeOf(group.dataset.sheet));
    const used = groups.filter(group => outcomeOf(group.dataset.sheet) === 'used').length;
    const scrapped = groups.filter(group => outcomeOf(group.dataset.sheet) === 'scrapped').length;
    const untouched = groups.filter(group => outcomeOf(group.dataset.sheet) === 'untouched').length;
    const offcuts = [...document.querySelectorAll('.remnant-fields')].reduce((sum, container) => {
      if (outcomeOf(container.dataset.sheet) !== 'used') return sum;
      return sum + [...container.querySelectorAll('[data-remnant-row]')]
        .filter(row => [...row.querySelectorAll('[data-dimension]')].some(input => input.value)).length;
    }, 0);
    const pieces = panels.filter(panel => outcomeOf(panel.dataset.sheetPk) === 'used')
      .reduce((sum, panel) => sum + number(panel.dataset.pieceCount), 0);
    const summary = `${fmt(used + scrapped)} ورق از انبار کسر می‌شود (${fmt(used)} مصرف‌شده، ${fmt(scrapped)} اسقاط) · ${fmt(untouched)} ورق دست‌نخورده برمی‌گردد · ${fmt(offcuts)} پرت واقعی افزوده می‌شود · ${fmt(pieces)} قطعه تکمیل می‌شود`;
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
    const container = document.getElementById(`remnants-${sheet}`);
    const note = document.getElementById(`note-${sheet}`);
    if (note) note.required = outcome === 'scrapped';
    if (container) {
      container.querySelectorAll('input[data-dimension]').forEach(input => { input.disabled = outcome !== 'used'; });
      renumber(container);
    }
    updateTabStatus(sheet);
    updateProgress();
    updateSummary();
  }

  function markInvalidPanel(field) {
    const panel = field.closest('[data-sheet-panel]');
    showPanel(panel);
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
        markInvalidPanel(panel.querySelector('input.cut-outcome'));
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
      ['اسقاط', summary.scrapped],
      ['برگشت به انبار', summary.untouched],
      ['پرت جدید', summary.offcuts],
      ['قطعه تکمیل‌شده', summary.pieces]
    ];
    return `<table class="table table-sm mb-0"><tbody>${rows.map(([label, value]) => `<tr><th scope="row">${label}</th><td class="text-end">${fmt(value)}</td></tr>`).join('')}</tbody></table>`;
  }

  document.querySelectorAll('.remnant-fields').forEach(container => renumber(container));
  panels.forEach(panel => updateTabStatus(panel.dataset.sheetPk));
  updateProgress();
  document.querySelectorAll('input.cut-outcome').forEach(radio => {
    radio.addEventListener('change', () => syncOutcome(radio.dataset.sheet));
  });
  document.querySelectorAll('.sheet-tab').forEach(tab => tab.addEventListener('click', () => {
    const panel = panels.find(item => item.dataset.sheetPk === tab.dataset.sheetPk);
    showPanel(panel);
  }));
  panels.forEach(panel => {
    panel.querySelector('.previous-sheet')?.addEventListener('click', () => showPanel(panels[panels.indexOf(panel) - 1]));
    panel.querySelector('.next-sheet')?.addEventListener('click', () => showPanel(panels[panels.indexOf(panel) + 1]));
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
    document.getElementById('confirm-cut-summary').innerHTML = renderSummaryTable(summary);
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
  document.querySelectorAll('[data-confirm-cancel]').forEach(form => form.addEventListener('submit', event => {
    if (!window.confirm('برنامه لغو و رزرو ورق‌ها آزاد شود؟ موجودی واقعی تغییر نمی‌کند.')) event.preventDefault();
  }));

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

  panels.forEach(panel => updateTabStatus(panel.dataset.sheetPk));
  const firstUnresolved = panels.find(panel => !outcomeOf(panel.dataset.sheetPk));
  showPanel(firstUnresolved || panels[0]);
  updateSummary();
})();
