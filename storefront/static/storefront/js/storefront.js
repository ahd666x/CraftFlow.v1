/*
 * Storefront utility functions.
 * Alpine components are now registered inline in the base template
 * to ensure they're available before Alpine initializes.
 */

/* Toast for HTMX events (like cartUpdated). */
document.addEventListener('cartUpdated', function (e) {
    var count = e.detail?.count;
    var host = document.getElementById('toast-host');
    if (!host) {
        return;
    }
    var toast = document.createElement('div');
    toast.className = 'card px-4 py-3 text-sm text-stone-800 animate-slide-up';
    toast.textContent = count === 0 ? 'سبد خرید خالی شد' : 'به سبد اضافه شد';
    host.appendChild(toast);
    setTimeout(function () {
        toast.classList.remove('animate-slide-up');
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(-16px)';
        setTimeout(function () { toast.remove(); }, 300);
    }, 2000);

    // آپدیت تمام شمارنده‌های سبد در صفحه
    if (typeof count === 'number') {
        document.querySelectorAll('#cart-count, [data-cart-count]').forEach(function (el) {
            el.textContent = count;
            el.classList.toggle('hidden', count === 0);
        });
    }
});

/* Toast for HTMX cart errors (e.g. out-of-range dimension on add-to-cart).
   Dispatched via the HX-Trigger response header (cartError). */
document.addEventListener('cartError', function (e) {
    var host = document.getElementById('toast-host');
    if (!host) {
        return;
    }
    var toast = document.createElement('div');
    toast.className = 'card px-4 py-3 text-sm text-red-700 border-red-200 bg-red-50 animate-slide-up';
    toast.textContent = e.detail || 'خطا در افزودن به سبد خرید';
    host.appendChild(toast);
    setTimeout(function () {
        toast.classList.remove('animate-slide-up');
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(-16px)';
        setTimeout(function () { toast.remove(); }, 300);
    }, 3000);
});

/* Quantity increment/decrement in cart rows — delegated event on
   `data-cart-step` buttons instead of inline scripts. */
document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-cart-step]') : null;
    if (!btn) {
        return;
    }
    var form = btn.closest('form');
    if (!form) {
        return;
    }
    var input = form.querySelector('input[name="quantity"]');
    if (!input) {
        return;
    }
    var delta = parseInt(btn.getAttribute('data-cart-step'), 10) || 0;
    var val = parseInt(input.value, 10) || 1;
    input.value = Math.max(1, val + delta);
    if (typeof htmx !== 'undefined') {
        htmx.trigger(form, 'submit');
    } else {
        form.requestSubmit();
    }
});

/* Quantity change on input in cart */
document.addEventListener('change', function (e) {
    var input = e.target.closest ? e.target.closest('.cart-qty-input') : null;
    if (!input) {
        return;
    }
    var form = input.closest('form');
    if (form) {
        if (typeof htmx !== 'undefined') {
            htmx.trigger(form, 'submit');
        } else {
            form.requestSubmit();
        }
    }
});

/* Product image gallery is handled by Alpine.js productGallery component in the template.
   No additional JavaScript needed for thumbnail click handling. */

/* Close mini-cart from outside (e.g. after going to checkout) */
document.addEventListener('close-mini-cart', function () {
    // Use the window event that header component already listens to
    window.dispatchEvent(new CustomEvent('close-mini-cart', { bubbles: true }));
});

/* Checkout: select saved address, fill full address in textarea.
   Data is placed in `<script type="application/json" id="saved-addresses">` by the view. */
(function () {
    var select = document.getElementById('address_id');
    var textarea = document.getElementById('shipping_address');
    var host = document.getElementById('saved-addresses');
    if (!select || !textarea || !host) {
        return;
    }
    var saved = {};
    try {
        saved = JSON.parse(host.textContent);
    } catch (err) {
        return;
    }
    select.addEventListener('change', function () {
        var value = this.value;
        if (value && saved[value]) {
            textarea.value = saved[value];
        }
    });
})();