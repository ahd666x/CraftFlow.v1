(function ($) {
    'use strict';

    function escapeHtml(v) {
        var d = document.createElement('div');
        d.textContent = (v === null || v === undefined) ? '' : String(v);
        return d.innerHTML;
    }

    function applyColorFields(sel, defaults, overwrite) {
        var allowed = Object.keys(defaults || {});
        document.querySelectorAll(sel).forEach(function (el) {
            var part = el.getAttribute('data-part');
            var select = el.querySelector('select');
            var isAllowed = allowed.length === 0 || allowed.indexOf(part) !== -1;
            el.style.display = isAllowed ? '' : 'none';
            if (!isAllowed && select) {
                select.value = '';
            } else if (isAllowed && overwrite && select) {
                select.value = defaults[part] || '';
            }
        });
    }

    function resetColorFields(sel) {
        document.querySelectorAll(sel).forEach(function (el) {
            el.style.display = '';
            var s = el.querySelector('select');
            if (s) s.value = '';
        });
    }

    window.initCategoryProductColorFilter = function (opts) {
        var $category = $(opts.categorySelector);
        var $product = $(opts.productSelector);
        var sel = opts.colorFieldSelector || '.color-field';
        var loadProductsUrl = opts.loadProductsUrl;
        var loadColorsUrlBase = opts.loadColorsUrlBase;
        var overwriteOnInitialLoad = opts.overwriteOnInitialLoad === true;

        function loadProducts(categoryId, selectedId) {
            if (!categoryId) {
                $product.html('<option value="">---------</option>');
                resetColorFields(sel);
                return;
            }
            $.ajax({
                url: loadProductsUrl,
                data: { category: categoryId },
                success: function (data) {
                    var html = '<option value="">---------</option>';
                    (data || []).forEach(function (p) {
                        html += '<option value="' + escapeHtml(p.id) + '">' + escapeHtml(p.name) + '</option>';
                    });
                    $product.html(html);
                    if (selectedId) {
                        $product.val(selectedId);
                    }
                }
            });
        }

        $category.on('change', function () {
            loadProducts($(this).val());
        });

        $product.on('change', function () {
            var pid = $(this).val();
            if (!pid) {
                resetColorFields(sel);
                return;
            }
            fetch(loadColorsUrlBase + pid + '/')
                .then(function (r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                })
                .then(function (data) {
                    applyColorFields(sel, data.defaults || {}, true);
                })
                .catch(function (err) {
                    console.error('Error loading product colors:', err);
                    resetColorFields(sel);
                });
        });

        var initialCategory = $category.val();
        var initialProduct = $product.val();

        if (initialCategory && !initialProduct) {
            loadProducts(initialCategory);
        }

        if (initialProduct) {
            var initialSelectedId = $product.data('initial-product') || initialProduct;
            if (initialCategory) {
                loadProducts(initialCategory, initialSelectedId);
            }
            fetch(loadColorsUrlBase + initialProduct + '/')
                .then(function (r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                })
                .then(function (data) {
                    applyColorFields(sel, data.defaults || {}, overwriteOnInitialLoad);
                })
                .catch(function (err) {
                    console.error('Error loading initial product colors:', err);
                });
        }
    };
})(jQuery);