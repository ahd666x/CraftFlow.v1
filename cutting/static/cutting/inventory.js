(() => {
  document.querySelectorAll('[data-deactivate-url]').forEach(button => button.addEventListener('click', () => {
    const form = document.getElementById('deactivate-form');
    form.action = button.dataset.deactivateUrl;
    bootstrap.Modal.getOrCreateInstance(document.getElementById('deactivate-modal')).show();
  }));

  const search = document.querySelector('[data-inventory-search]');
  const form = search?.form;
  if (search && form) {
    let timer;
    search.addEventListener('input', () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => form.requestSubmit(), 500);
    });
  }
})();
