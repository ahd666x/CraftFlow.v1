/*
 * اجزای اختصاصی فروشگاه.
 *
 * dimensionPrice: محاسبه‌ی زنده‌ی قیمت در صفحه‌ی جزئیات محصول.
 * heroSlider: اسلایدر هیرو با Ken Burns effect
 * scrollReveal: انیمیشن‌های ظهور با IntersectionObserver
 * parallax: افکت پارالاکس برای تصاویر
 * floatingElements: عناصر شناور
 * productCard: تعاملات کارت محصول
 * headerScroll: حالت هدر هنگام اسکرول
 */

document.addEventListener('alpine:init', function () {
    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    Alpine.directive('reveal', function (el, { modifiers }) {
        el.classList.add('reveal');
        var i = modifiers.indexOf('delay');
        if (i !== -1 && modifiers[i + 1]) {
            el.style.transitionDelay = modifiers[i + 1] + 'ms';
        }
        if (reduce || !('IntersectionObserver' in window)) {
            el.classList.add('is-visible');
            return;
        }
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (e.isIntersecting) {
                    e.target.classList.add('is-visible');
                    io.unobserve(e.target);
                }
            });
        }, { threshold: 0.15 });
        io.observe(el);
    });

    Alpine.data('heroSlider', function (count) {
        return {
            current: 0,
            timer: null,
            paused: false,
            init: function () {
                if (reduce || count < 2) return;
                var self = this;
                this.timer = setInterval(function () {
                    if (!self.paused && !document.hidden) self.next();
                }, 6000);
            },
            destroy: function () { clearInterval(this.timer); },
            next: function () { this.current = (this.current + 1) % count; },
            prev: function () { this.current = (this.current - 1 + count) % count; },
            go: function (i) { this.current = i; },
        };
    });

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

            diffFor: function (axis) {
                var d = this.dims[axis];
                if (!d.editable || d.def === null || d.value === null || d.value === '') {
                    return 0;
                }
                return Number(d.value) - d.def;
            },

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

    // Scroll Reveal Component
    Alpine.data('scrollReveal', function () {
        return {
            observer: null,
            elements: [],

            init: function () {
                var self = this;
                this.elements = Array.from(this.$el.querySelectorAll('.reveal'));

                if ('IntersectionObserver' in window) {
                    this.observer = new IntersectionObserver(function (entries) {
                        entries.forEach(function (entry) {
                            if (entry.isIntersecting) {
                                entry.target.classList.add('visible');
                                self.observer.unobserve(entry.target);
                            }
                        });
                    }, {
                        root: null,
                        rootMargin: '0px 0px -10% 0px',
                        threshold: 0.1
                    });

                    this.elements.forEach(function (el) {
                        self.observer.observe(el);
                    });
                } else {
                    // Fallback for older browsers
                    this.elements.forEach(function (el) {
                        el.classList.add('visible');
                    });
                }
            },

            destroy: function () {
                if (this.observer) {
                    this.observer.disconnect();
                }
            }
        };
    });

    // Parallax Component
    Alpine.data('parallax', function (config) {
        return {
            speed: config?.speed || 0.3,
            element: null,

            init: function () {
                this.element = this.$el;
                window.addEventListener('scroll', this.handleScroll.bind(this), { passive: true });
            },

            handleScroll: function () {
                var rect = this.element.getBoundingClientRect();
                var viewportHeight = window.innerHeight;

                if (rect.bottom < 0 || rect.top > viewportHeight) {
                    return; // Element not in viewport
                }

                var scrollProgress = (viewportHeight - rect.top) / (viewportHeight + rect.height);
                var translateY = scrollProgress * 100 * this.speed;

                this.element.style.transform = "translateY(" + translateY + "px)";
            },

            destroy: function () {
                window.removeEventListener('scroll', this.handleScroll.bind(this));
            }
        };
    });

    // Floating Element Component
    Alpine.data('floatingElement', function (config) {
        return {
            amplitude: config?.amplitude || 16,
            duration: config?.duration || 6000,
            delay: config?.delay || 0,
            startTime: null,

            init: function () {
                this.startTime = Date.now() + this.delay;
                requestAnimationFrame(this.animate.bind(this));
            },

            animate: function () {
                var elapsed = Date.now() - this.startTime;
                var progress = (elapsed % this.duration) / this.duration;
                var y = Math.sin(progress * Math.PI * 2) * this.amplitude;

                this.$el.style.transform = "translateY(" + y + "px)";
                requestAnimationFrame(this.animate.bind(this));
            }
        };
    });

    // Product Card Interactions
    Alpine.data('productCard', function () {
        return {
            showQuickAdd: false,

            init: function () {
                var self = this;
                this.$el.addEventListener('mouseenter', function () {
                    self.showQuickAdd = true;
                });
                this.$el.addEventListener('mouseleave', function () {
                    self.showQuickAdd = false;
                });
            }
        };
    });

    // Header Scroll Effect
    Alpine.data('headerScroll', function () {
        return {
            scrolled: false,
            threshold: 100,

            init: function () {
                window.addEventListener('scroll', this.handleScroll.bind(this), { passive: true });
            },

            handleScroll: function () {
                this.scrolled = window.scrollY > this.threshold;
            },

            destroy: function () {
                window.removeEventListener('scroll', this.handleScroll.bind(this));
            }
        };
    });

    // Custom Dimensions Demo (for homepage)
    Alpine.data('customDimsDemo', function () {
        return {
            length: 100,
            width: 50,
            height: 80,
            basePrice: 5000000,
            lengthPercent: 0.5,
            widthPercent: 0.4,
            heightPercent: 0.3,

            unitPrice: function () {
                var diffs = 0;
                diffs += (this.length - 100) * this.lengthPercent;
                diffs += (this.width - 50) * this.widthPercent;
                diffs += (this.height - 80) * this.heightPercent;
                var finalPrice = this.basePrice + this.basePrice * (diffs / 100);
                return finalPrice < 0 ? 0 : Math.round(finalPrice);
            },

            total: function () {
                return this.unitPrice();
            }
        };
    });

    // Testimonial Slider
    Alpine.data('testimonialSlider', function () {
        return {
            currentIndex: 0,
            items: [],
            intervalId: null,

            init: function () {
                this.items = Array.from(this.$el.querySelectorAll('.testimonial-slider__item'));
                if (this.items.length > 1) {
                    this.startAutoPlay();
                }
            },

            goTo: function (index) {
                if (index < 0) index = this.items.length - 1;
                if (index >= this.items.length) index = 0;
                this.currentIndex = index;
                this.updateSlider();
            },

            next: function () {
                this.goTo(this.currentIndex + 1);
            },

            prev: function () {
                this.goTo(this.currentIndex - 1);
            },

            updateSlider: function () {
                var container = this.$el.querySelector('.testimonial-slider__track');
                if (container) {
                    container.style.transform = "translateX(-" + (this.currentIndex * 100) + "%)";
                }
            },

            startAutoPlay: function () {
                var self = this;
                this.intervalId = setInterval(function () {
                    self.next();
                }, 6000);
            },

            destroy: function () {
                clearInterval(this.intervalId);
            }
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
    toast.className = 'card px-4 py-3 text-sm text-stone-800 animate-slide-up';
    toast.textContent = 'به سبد اضافه شد';
    host.appendChild(toast);
    setTimeout(function () {
        toast.classList.remove('animate-slide-up');
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(-16px)';
        setTimeout(function () { toast.remove(); }, 300);
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

/* تغییر تعداد با Eingabe در input تعداد سبد */
document.addEventListener('change', function (e) {
    var input = e.target.closest ? e.target.closest('.cart-qty-input') : null;
    if (!input) {
        return;
    }
    var form = input.closest('form');
    if (form) {
        form.submit();
    }
});

/* گالری تصاویر محصول — کلیک روی تومبنیل */
document.addEventListener('click', function (e) {
    var thumb = e.target.closest ? e.target.closest('.gallery-thumb') : null;
    if (!thumb) {
        return;
    }
    var mainImg = document.getElementById('gallery-main');
    if (mainImg) {
        mainImg.src = thumb.getAttribute('data-img-src');
    }
});

/* مینی‌سبد (Mini Cart Drawer) */
function registerMiniCart() {
    Alpine.data('miniCart', function () {
        return {
            loaded: false,
            miniCartUrl: '',
            init: function () {
                // Store URL from data attribute during initialization
                this.miniCartUrl = this.$el.getAttribute('data-mini-cart-url') || '/cart/mini/';
            },
            loadContent: function () {
                if (this.loaded) return;
                var drawerContent = document.getElementById('mini-cart-content');
                if (!drawerContent) return;

                fetch(this.miniCartUrl, {
                    headers: { 'HX-Request': 'true' }
                })
                .then(function (response) { return response.text(); })
                .then(function (html) {
                    drawerContent.innerHTML = html;
                    this.loaded = true;
                    if (window.Alpine) {
                        window.Alpine.discoverComponents(drawerContent);
                    }
                }.bind(this))
                .catch(function (err) {
                    console.error('Mini cart load failed:', err);
                    drawerContent.innerHTML = '<p class="text-center text-red-600 p-4">خطا در بارگذاری سبد</p>';
                });
            },
            close: function () {
                this.$dispatch('close-mini-cart');
            }
        };
    });
}

// Register immediately if Alpine is already available, otherwise wait for alpine:init
if (window.Alpine) {
    registerMiniCart();
} else {
    document.addEventListener('alpine:init', registerMiniCart);
}

/* بستن مینی‌سبد از خارج (مثلاً بعد از رفتن به تسویه) */
document.addEventListener('close-mini-cart', function () {
    var header = document.querySelector('header');
    if (header && header.__x) {
        var data = header.__x.getUnobservedData();
        if (data) {
            data.cartOpen = false;
        }
    }
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