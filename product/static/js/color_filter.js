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
            if (select && typeof window.updateSwatch === 'function') {
                window.updateSwatch(select);
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

    var AXES = ['length', 'width', 'height'];

    function applyDimensions($product, opts) {
        var $o = $product.find('option:selected');
        var hasSelection = !!$o.val();

        AXES.forEach(function (axis) {
            var $f = $('#id_' + axis);
            if (!$f.length) return;
            var def = hasSelection ? ($o.data(axis) || '') : '';
            var prevAuto = $f.data('auto');                 // previous default we set
            var cur = $f.val();
            // Only overwrite when the field is empty or still holds the
            // previous auto-filled default — manual edits are preserved.
            if (cur === '' || String(cur) === String(prevAuto || '')) {
                $f.val(def);
            }
            $f.data('auto', def);
            var editable = $o.data(axis + 'Editable') !== false;
            $f.prop('readonly', !!def && !editable);
            $f.closest('.dim-field').toggle(!!def || hasSelection);
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
                AXES.forEach(function (axis) {
                    var $f = $('#id_' + axis);
                    if ($f.length) {
                        $f.val('');
                        $f.data('auto', '');
                        $f.closest('.dim-field').show();
                    }
                });
                return;
            }
            $.ajax({
                url: loadProductsUrl,
                data: { category: categoryId },
                success: function (data) {
                    var html = '<option value="">---------</option>';
                    (data || []).forEach(function (p) {
                        var opt = '<option value="' + escapeHtml(p.id) + '"';
                        AXES.forEach(function (axis) {
                            if (p[axis] !== null && p[axis] !== undefined) {
                                opt += ' data-' + axis + '="' + escapeHtml(p[axis]) + '"';
                            }
                        });
                        opt += ' data-' + 'length' + '-editable="' + escapeHtml(p.length_editable) + '"';
                        opt += ' data-' + 'width' + '-editable="' + escapeHtml(p.width_editable) + '"';
                        opt += ' data-' + 'height' + '-editable="' + escapeHtml(p.height_editable) + '"';
                        opt += '>' + escapeHtml(p.name) + '</option>';
                        html += opt;
                    });
                    $product.html(html);
                    if (selectedId) {
                        $product.val(selectedId);
                        $product.trigger('change', [{ initial: true }]);
                    }
                }
            });
        }

        $category.on('change', function () {
            loadProducts($(this).val());
        });

        $product.on('change', function (e, extra) {
            var isInitial = extra && extra.initial;
            var overwriteColors = isInitial ? overwriteOnInitialLoad : true;
            applyDimensions($product, opts);
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
                .then(function (d) {
                    applyColorFields(sel, d.defaults || {}, overwriteColors);
                })
                .catch(function (err) {
                    console.error(err);
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
        }
    };
})(jQuery);