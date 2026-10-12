/*
 * اجزای اختصاصی فروشگاه.
 *
 * dimensionPrice: محاسبه‌ی زنده‌ی قیمت در صفحه‌ی جزئیات محصول.
 * فرمول دقیقاً همان storefront/pricing.py است تا قیمت نمایش‌داده‌شده
 * با قیمتی که در سبد ذخیره می‌شود هرگز تفاوت نکند.
 */
document.addEventListener('alpine:init', function () {
    Alpine.data('dimensionPrice', function (config) {
        var axes = ['length', 'width', 'height'];

        function toAxis(key, spec) {
            return {
                value: null,
                def: (spec.def === null || spec.def === undefined) ? null : Number(spec.def),
                editable: !!spec.editable,
                percent: Number(spec.percent || 0),
            };
        }

        return {
            basePrice: Number(config.basePrice || 0),
            dims: {
                length: toAxis('length', config.dims.length),
                width: toAxis('width', config.dims.width),
                height: toAxis('height', config.dims.height),
            },
            quantity: 1,

            // همان _dimension_diff در pricing.py
            diffFor: function (axis) {
                var d = this.dims[axis];
                if (!d.editable || d.def === null || d.value === null || d.value === '') {
                    return 0;
                }
                return Number(d.value) - d.def;
            },

            // همان calculate_dimension_price در pricing.py
            unitPrice: function () {
                var diffs = 0;
                for (var i = 0; i < axes.length; i++) {
                    var axis = axes[i];
                    diffs += this.diffFor(axis) * this.dims[axis].percent;
                }
                var finalPrice = this.basePrice + this.basePrice * (diffs / 100);
                return finalPrice < 0 ? 0 : Math.round(finalPrice);
            },

            total: function () {
                var qty = parseInt(this.quantity, 10);
                if (!qty || qty < 1) {
                    qty = 1;
                }
                return this.unitPrice() * qty;
            },

            recalc: function () {
                // placeholder برای @input؛ Alpine به‌صورت خودکار
                // total() را در x-text به‌روز می‌کند.
            },
        };
    });
});

/* Toast ساده برای رویدادهای HTMX (مثل cartUpdated). */
document.addEventListener('cartUpdated', function () {
    var host = document.getElementById('toast-host');
    if (!host) {
        return;
    }
    var toast = document.createElement('div');
    toast.className = 'card px-4 py-3 text-sm text-stone-800';
    toast.textContent = 'به سبد اضافه شد';
    host.appendChild(toast);
    setTimeout(function () {
        toast.remove();
    }, 2000);
});

/* کاهش/افزایش تعداد در سطرهای سبد — رویداد تفویضی روی دکمه‌های
   `data-cart-step` به‌جای اسکریپت تکراری در هر سطر. */
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
    form.submit();
});

/* تسویه: انتخاب آدرس ذخیره‌شده، آدرس کامل را در textarea پر می‌کند.
   داده‌ها در تگ `<script type="application/json" id="saved-addresses">`
   توسط view گذاشته می‌شوند. */
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
