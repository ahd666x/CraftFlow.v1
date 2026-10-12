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

/* منوی موبایل — دکمه در هدر، پنل زیر آن با Alpine.
   این جا فقط رفتار دکمه را به رویداد سفارشی وصل می‌کند تا
   در صورت عدم وجود Alpine، منو باز/بسته نشود. */
(function () {
    var toggle = document.getElementById('mobile-menu-toggle');
    if (!toggle) {
        return;
    }
    toggle.addEventListener('click', function () {
        document.dispatchEvent(new CustomEvent('mobile-menu-toggle'));
        var icon = document.getElementById('menu-icon');
        if (icon) {
            var open = toggle.getAttribute('aria-expanded') === 'true';
            toggle.setAttribute('aria-expanded', !open);
            if (open) {
                icon.setAttribute('d', 'M6 6l12 12M6 18L18 6');
            } else {
                icon.setAttribute('d', 'M4 6h16M4 12h16M4 18h16');
            }
        }
    });
})();