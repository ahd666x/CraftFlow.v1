/*
 * Storefront utility functions.
 * Alpine components are registered inline in the base template
 * to ensure they're available before Alpine initializes.
 */

/* --- Cart counter helpers ------------------------------------------------ */

function updateCartCounters(count) {
    if (typeof count !== 'number') return;
    // هدر id="cart-count" دارد؛ ناوبری موبایل data-cart-count (id تکراری ممنوع)
    document.querySelectorAll('#cart-count, [data-cart-count]').forEach(function (el) {
        el.textContent = count;
        el.classList.toggle('hidden', count === 0);
    });
}

function showToast(text, variant) {
    var host = document.getElementById('toast-host');
    if (!host) return;
    var toast = document.createElement('div');
    toast.className = 'card px-4 py-3 text-sm animate-slide-up ' + (
        variant === 'error'
            ? 'text-red-700 border-red-200 bg-red-50'
            : variant === 'success'
                ? 'text-emerald-800 border-emerald-200 bg-emerald-50'
                : 'text-stone-800'
    );
    toast.textContent = text;
    host.appendChild(toast);
    setTimeout(function () {
        toast.classList.remove('animate-slide-up');
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(-16px)';
        setTimeout(function () { toast.remove(); }, 300);
    }, 2500);
}

/* Counter + toast for HTMX cart events (add/update/remove/clear/discount).
   `silent` پیام عمومی «به سبد اضافه شد» را برای عملیات‌های دیگر خاموش می‌کند. */
document.addEventListener('cartUpdated', function (e) {
    var detail = e.detail || {};
    updateCartCounters(detail.count);
    if (!detail.silent) {
        showToast(detail.count === 0 ? 'سبد خرید خالی شد' : 'به سبد اضافه شد');
    }
});

/* Neutral message toast (discount applied/removed, review submitted, ...)
   sent via the HX-Trigger response header — بدون آلوده‌کردن سشن پیام‌ها. */
document.addEventListener('cartMessage', function (e) {
    if (e.detail) showToast(e.detail, 'success');
});

/* Toast for HTMX cart errors (e.g. out-of-range dimension on add-to-cart)
   — جایگزین alert(). از فرم‌ها هم با CustomEvent dispatch می‌شود. */
document.addEventListener('cartError', function (e) {
    showToast(e.detail || 'خطا در افزودن به سبد خرید', 'error');
});

/* Fallback sync: بعد از هر swap ناحیه‌های سبد، شمارنده را از data-cart-total
   داخل محتوای تازه همگام کن (اگر HX-Trigger به هر دلیل نرسیده باشد).
   پاسخ «افزودن سریع» خودِ #cart-count را swap می‌کند؛ هدف را هم می‌خوانیم
   تا بج موبایل (data-cart-count) همگام بماند. */
document.addEventListener('htmx:afterSwap', function (e) {
    var target = e.detail && e.detail.target;
    if (!target) return;
    if (target.id === 'cart-count') {
        var n = parseInt(target.textContent, 10);
        if (!isNaN(n)) updateCartCounters(n);
        return;
    }
    if (target.id === 'mini-cart-content' || target.id === 'cart-rows') {
        var holder = target.querySelector('[data-cart-total]');
        if (holder) {
            var count = parseInt(holder.getAttribute('data-cart-total'), 10);
            if (!isNaN(count)) updateCartCounters(count);
        }
    }
});

/* --- Cart quantity inputs ------------------------------------------------ */

var qtyTimer = null;

function submitCartForm(form) {
    if (typeof htmx !== 'undefined') {
        htmx.trigger(form, 'submit');
    } else {
        form.requestSubmit();
    }
}

/* Quantity increment/decrement in cart rows — immediate. */
document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-cart-step]') : null;
    if (!btn) return;
    var form = btn.closest('form');
    if (!form) return;
    var input = form.querySelector('input[name="quantity"]');
    if (!input) return;
    var delta = parseInt(btn.getAttribute('data-cart-step'), 10) || 0;
    var val = parseInt(input.value, 10) || 1;
    input.value = Math.max(1, val + delta);
    submitCartForm(form);
});

/* Typed quantity changes debounce ۶۰۰ms — هر کلید درخواست نمی‌فرستد.
   change (blur/Enter) بلافاصله ارسال می‌کند. */
document.addEventListener('input', function (e) {
    var input = e.target.closest ? e.target.closest('.cart-qty-input') : null;
    if (!input) return;
    var form = input.closest('form');
    if (!form) return;
    clearTimeout(qtyTimer);
    qtyTimer = setTimeout(function () { submitCartForm(form); }, 600);
});
document.addEventListener('change', function (e) {
    var input = e.target.closest ? e.target.closest('.cart-qty-input') : null;
    if (!input) return;
    var form = input.closest('form');
    if (!form) return;
    clearTimeout(qtyTimer);
    submitCartForm(form);
});

/* Product image gallery is handled by Alpine.js productGallery component in the template. */

/* --- Checkout: saved addresses ------------------------------------------- */

/* Select saved address → fill address, recipient (name) and city in textarea.
   Data is placed in `<script type="application/json" id="saved-addresses">` by the view:
   { "<pk>": { "address": "...", "recipient": "...", "city": "..." }, ... }
   If the textarea already has content, replacement is confirmed first. */
(function () {
    var select = document.getElementById('address_id');
    var textarea = document.getElementById('shipping_address');
    var host = document.getElementById('saved-addresses');
    if (!select || !textarea || !host) return;

    var saved = {};
    try {
        saved = JSON.parse(host.textContent);
    } catch (err) {
        return;
    }

    select.addEventListener('change', function () {
        var value = this.value;
        var data = saved[value];
        if (!value || !data) return;

        var full = data.city ? (data.city + '، ' + data.address) : data.address;
        var current = textarea.value.trim();

        if (current && current !== full && current !== data.address) {
            if (!window.confirm('آدرس ذخیره‌شده جایگزین متن فعلی شود؟')) {
                this.value = '';
                return;
            }
        }

        textarea.value = full;

        var nameInput = document.getElementById('name');
        if (nameInput && data.recipient && !nameInput.value.trim()) {
            nameInput.value = data.recipient;
        }
    });
})();
