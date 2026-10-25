(() => {
  const form = document.getElementById('stock-form');
  if (!form) return;

  const material = document.getElementById('material');
  const typeInputs = [...document.querySelectorAll('input[name="stock_type"]')];
  const mode = document.getElementById('dimension_mode');
  const modeWrap = document.getElementById('dimension-mode-wrap');
  const specWrap = document.getElementById('standard-size-wrap');
  const customWrap = document.getElementById('custom-size-wrap');
  const spec = document.getElementById('size_spec');
  const width = document.getElementById('width');
  const height = document.getElementById('height');
  const quickRepeatWrap = document.getElementById('quick-repeat-wrap');
  let wasStandard = null;

  const selectedType = () => document.querySelector('input[name="stock_type"]:checked')?.value || 'full';

  function syncStockTypeAndDimensions() {
    if (!mode || !spec || !width || !height) return;
    const isOffcut = selectedType() === 'offcut';
    if (isOffcut) mode.value = 'custom';
    if (modeWrap) modeWrap.hidden = isOffcut;
    if (quickRepeatWrap) quickRepeatWrap.hidden = !isOffcut;

    const isStandard = !isOffcut && mode.value === 'standard';
    if (specWrap) specWrap.hidden = !isStandard;
    spec.disabled = !isStandard;
    customWrap.hidden = isStandard;
    width.disabled = isStandard;
    height.disabled = isStandard;
    width.required = !isStandard;
    height.required = !isStandard;

    if (!isStandard && wasStandard === true) {
      width.value = '';
      height.value = '';
    }

    for (const option of spec.options) {
      // option.hidden در Safari/iOS پشتیبانی نمی‌شود؛ disabled همه‌جا کار می‌کند
      // (بازبینی UI/UX بند ۵) — گزینه‌های متریال دیگر غیرفعال می‌شوند، نه مخفی.
      if (option.value) option.disabled = option.dataset.material !== material.value;
    }
    if (isStandard) {
      let selected = spec.selectedOptions[0];
      if (!selected || selected.dataset.material !== material.value) {
        selected = [...spec.options].find(option => option.dataset.material === material.value && option.dataset.default === '1');
        spec.value = selected ? selected.value : '';
      }
      if (spec.value) {
        const current = spec.selectedOptions[0];
        width.value = current.dataset.width;
        height.value = current.dataset.height;
      } else {
        width.value = '';
        height.value = '';
      }
    } else {
      spec.value = '';
    }
    wasStandard = isStandard;
  }

  if (material) material.addEventListener('change', syncStockTypeAndDimensions);
  typeInputs.forEach(input => input.addEventListener('change', syncStockTypeAndDimensions));
  if (mode) mode.addEventListener('change', syncStockTypeAndDimensions);
  if (spec) spec.addEventListener('change', () => {
    const selected = spec.selectedOptions[0];
    if (selected && selected.value) {
      width.value = selected.dataset.width;
      height.value = selected.dataset.height;
    }
  });
  syncStockTypeAndDimensions();

  if (form.dataset.edit === '1') {
    const reasonWrap = document.getElementById('change-reason-wrap');
    const reason = document.getElementById('change_reason');
    const tracked = [...form.querySelectorAll('[data-initial]')];
    const syncReason = () => {
      const changed = tracked.some(input => input.value !== input.dataset.initial);
      reasonWrap.hidden = !changed;
      reason.required = changed;
    };
    tracked.forEach(input => {
      input.addEventListener('input', syncReason);
      input.addEventListener('change', syncReason);
    });
    syncReason();
  }
})();
