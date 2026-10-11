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

    function fillSizeField(sizeInput, defaultSize) {
        if (!sizeInput) return;
        // Only pre-fill when the field is empty so the user's manual edits
        // are not wiped when they switch products.
        if (sizeInput.value.trim() === '' && defaultSize) {
            sizeInput.value = defaultSize;
        }
    }

    function readSelectedDefaultSize($product) {
        var selected = $product.find('option[value="' + $product.val() + '"]');
        if (!selected.length) return '';
        return selected.data('default-size') || '';
    }

    window.initCategoryProductColorFilter = function (opts) {
        var $category = $(opts.categorySelector);
        var $product = $(opts.productSelector);
        var sel = opts.colorFieldSelector || '.color-field';
        var loadProductsUrl = opts.loadProductsUrl;
        var loadColorsUrlBase = opts.loadColorsUrlBase;
        var overwriteOnInitialLoad = opts.overwriteOnInitialLoad === true;
        var $sizeField = $(opts.sizeFieldSelector || '#id_size');

        function loadProducts(categoryId, selectedId) {
            if (!categoryId) {
                $product.html('<option value="">---------</option>');
                resetColorFields(sel);
                fillSizeField($sizeField.get(0), '');
                return;
            }
            $.ajax({
                url: loadProductsUrl,
                data: { category: categoryId },
                success: function (data) {
                    var html = '<option value="">---------</option>';
                    (data || []).forEach(function (p) {
                        var opt = '<option value="' + escapeHtml(p.id) + '"';
                        if (p.default_size) {
                            opt += ' data-default-size="' + escapeHtml(p.default_size) + '"';
                        }
                        opt += '>' + escapeHtml(p.name) + '</option>';
                        html += opt;
                    });
                    $product.html(html);
                    if (selectedId) {
                        $product.val(selectedId);
                        $product.trigger('change');
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
                fillSizeField($sizeField.get(0), '');
                return;
            }
            // Prefer the default_size embedded in the product option (no extra
            // request needed). Fall back to the dedicated dimensions endpoint
            // if it was provided.
            var defaultSize = readSelectedDefaultSize($product);
            var colorPromise = fetch(loadColorsUrlBase + pid + '/')
                .then(function (r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                });

            var dimPromise;
            if (defaultSize) {
                dimPromise = Promise.resolve({ default_size: defaultSize });
            } else if (opts.loadProductDimensionsUrl) {
                dimPromise = fetch(opts.loadProductDimensionsUrl + '?product_id=' + encodeURIComponent(pid))
                    .then(function (r) {
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        return r.json();
                    });
            } else {
                dimPromise = Promise.resolve({});
            }

            Promise.all([colorPromise, dimPromise])
                .then(function (results) {
                    var colorData = results[0] || {};
                    var dimData = results[1] || {};
                    applyColorFields(sel, colorData.defaults || {}, true);
                    fillSizeField($sizeField.get(0), dimData.default_size || '');
                })
                .catch(function (err) {
                    console.error('Error loading product data:', err);
                    resetColorFields(sel);
                    fillSizeField($sizeField.get(0), '');
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
            var colorPromise = fetch(loadColorsUrlBase + initialProduct + '/')
                .then(function (r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                });

            var defaultSize = readSelectedDefaultSize($product);
            var dimPromise;
            if (defaultSize) {
                dimPromise = Promise.resolve({ default_size: defaultSize });
            } else if (opts.loadProductDimensionsUrl) {
                dimPromise = fetch(opts.loadProductDimensionsUrl + '?product_id=' + encodeURIComponent(initialProduct))
                    .then(function (r) {
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        return r.json();
                    });
            } else {
                dimPromise = Promise.resolve({});
            }

            Promise.all([colorPromise, dimPromise])
                .then(function (results) {
                    var colorData = results[0] || {};
                    var dimData = results[1] || {};
                    applyColorFields(sel, colorData.defaults || {}, overwriteOnInitialLoad);
                    fillSizeField($sizeField.get(0), dimData.default_size || '');
                })
                .catch(function (err) {
                    console.error('Error loading initial product colors:', err);
                });
        }
    };
})(jQuery);