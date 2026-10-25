(() => {
  document.querySelectorAll('[data-deactivate-url]').forEach(button => button.addEventListener('click', () => {
    const form = document.getElementById('deactivate-form');
    form.action = button.dataset.deactivateUrl;
    bootstrap.Modal.getOrCreateInstance(document.getElementById('deactivate-modal')).show();
  }));

  // جست‌وجو فقط با Enter یا دکمه «اعمال» ارسال می‌شود؛
  // ارسال خودکار وسط تایپ، فوکوس و مکان‌نما را می‌بُرد (بازبینی UI/UX بند ۲).
  const search = document.querySelector('[data-inventory-search]');
  const form = search?.form;
  if (search && form) {
    form.addEventListener('submit', () => search.blur());
  }
})();
