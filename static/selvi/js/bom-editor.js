/* bom-editor.js — سلوی چوب
   BOM (Bill of Materials) editor: dynamic rows, part create/edit modal,
   conditional size-adjustment rule dropdowns.

   Depends on jQuery. Reads URLs from data attributes on #bom-table:
     data-url-create-part  → URL for creating a new part
     data-url-edit-part   → base URL for editing (expects ?id= or suffix)
     data-url-get-part    → base URL for fetching part data (expects ?id=)
*/

(function ($) {
    'use strict';

    var BomEditor = {
        init: function () {
            this.bindEvents();
            this.initRuleDropdowns();
            this.loadPartModalFields();
        },

        bindEvents: function () {
            var self = this;

            // ---------- افزودن ردیف جدید BOM ----------
            $('#add-bom-row').off('click').on('click', function () {
                self.addRow();
            });

            // ---------- باز کردن مودال برای ایجاد ----------
            $(document).off('click', '.add-part-btn').on('click', '.add-part-btn', function () {
                self.currentRow = $(this).closest('.bom-row');
                self.resetModalToCreate();
                self.fillHiddenFields();
                $('#partModal').modal('show');
            });

            // ---------- باز کردن مودال برای ویرایش ----------
            $(document).off('click', '.edit-part-btn').on('click', '.edit-part-btn', function () {
                self.currentRow = $(this).closest('.bom-row');
                var partId = self.currentRow.data('part-id');
                if (!partId) return;

                var url = self.getPartUrl(partId);
                $.getJSON(url, function (data) {
                    $('#partModalTitle').text('ویرایش قطعه');
                    $('#part-submit-btn').text('ذخیره تغییرات');
                    self.editMode = true;
                    self.editPartId = partId;

                    $('#part-name').val(data.name);
                    $('#part-material').val(data.material);
                    $('#part-length').val(data.length);
                    $('#part-width').val(data.width);
                    $('#part-turn').prop('checked', data.turn);
                    $('#part-f26').val(data.f26);
                    $('#part-f18').val(data.f18);
                    $('#part-f4').val(data.f4);
                    $('#part-f5').val(data.f5);
                    $('#part-f3').val(data.f3);
                    $('#part-routing').val(data.routing_code);
                    $('#part-base').val(data.base_part);
                    $('#part-grain').val(data.grain);
                    $('#part-pname').val(data.pname);

                    $('#partModal').modal('show');
                });
            });

            // ---------- بستن مودال ----------
            $('#partModal').off('hidden.bs.modal').on('hidden.bs.modal', function () {
                self.currentRow = null;
                self.resetModalToCreate();
            });

            // ---------- ارسال فرم مودال ----------
            $('#part-form').off('submit').on('submit', function (e) {
                e.preventDefault();
                self.submitPartForm();
            });
        },

        initRuleDropdowns: function () {
            var self = this;
            $('#bom-rows tr.bom-row').each(function () {
                self.applyRuleDropdown(this);
            });
        },

        resetModalToCreate: function () {
            $('#partModalTitle').text('ایجاد قطعه جدید');
            $('#part-submit-btn').text('ایجاد قطعه');
            $('#part-form')[0].reset();
            this.editMode = false;
            this.editPartId = null;
        },

        fillHiddenFields: function () {
            var categoryName = $('#id_category option:selected').text().trim();
            var productName = $('#id_name').val().trim();
            $('#part-grain').val(categoryName);
            $('#part-pname').val(productName);
        },

        submitPartForm: function () {
            var self = this;
            if (!this.currentRow) {
                alert('ردیف انتخاب نشده است.');
                return;
            }

            var url = this.editMode
                ? this.editPartUrl(this.editPartId)
                : $('#bom-table').data('url-create-part');

            $.ajax({
                url: url,
                type: 'POST',
                data: $('#part-form').serialize(),
                success: function (response) {
                    if (response.success) {
                        self.currentRow.find('input[name$="-part"]').val(response.id);
                        self.currentRow.find('.part-name').text(response.name);
                        self.currentRow.data('part-id', response.id);
                        self.currentRow.find('.add-part-btn').remove();
                        if (!self.currentRow.find('.edit-part-btn').length) {
                            var editBtn = '<button type="button" class="btn btn-sm btn-outline-warning edit-part-btn" title="ویرایش قطعه"><i class="bi bi-pencil"></i></button>';
                            self.currentRow.find('.part-cell').append(editBtn);
                        }
                        $('#partModal').modal('hide');
                    } else {
                        alert('خطا: ' + JSON.stringify(response.errors));
                    }
                }
            });
        },

        getPartUrl: function (partId) {
            return $('#bom-table').data('url-get-part') || '/ajax/get-part/'.replace(/\/$/, '') + '/' + partId + '/';
        },

        editPartUrl: function (partId) {
            return '/ajax/edit-part/' + partId + '/';
        },

        applyRuleDropdown: function (row) {
            var $row = $(row);
            var ruleHidden = $row.find('.size-rule-hidden');
            var presetSelect = $row.find('.size-rule-preset');
            var customInput = $row.find('.size-rule-custom');

            function syncRuleField() {
                var presetVal = presetSelect.val();
                if (!presetVal) {
                    presetSelect.show();
                    customInput.hide();
                    ruleHidden.val('');
                    return;
                }
                if (presetVal === 'custom') {
                    presetSelect.hide();
                    customInput.show();
                    ruleHidden.val(customInput.val());
                } else {
                    presetSelect.show();
                    customInput.hide();
                    ruleHidden.val(presetVal);
                }
            }

            presetSelect.off('change').on('change', syncRuleField);
            customInput.off('input').on('input', function () {
                if (presetSelect.val() === 'custom') {
                    ruleHidden.val(customInput.val());
                }
            });

            if (ruleHidden.val() && presetSelect.length) {
                var options = Array.from(presetSelect[0].options);
                var matched = options.find(function (opt) { return opt.value === ruleHidden.val(); });
                if (matched) {
                    presetSelect.val(ruleHidden.val());
                } else {
                    presetSelect.val('custom');
                    customInput.val(ruleHidden.val());
                }
            }
            syncRuleField();
        },

        buildEmptyRowHtml: function (index) {
            return '' +
                '<tr class="bom-row" data-part-id="">' +
                    '<td class="part-cell">' +
                        '<span class="part-name">قطعه‌ای انتخاب نشده</span>' +
                        '<button type="button" class="btn btn-sm btn-outline-success add-part-btn" title="ایجاد قطعه جدید">' +
                            '<i class="bi bi-plus"></i>' +
                        '</button>' +
                        '<input type="hidden" name="bom-' + index + '-part" id="id_bom-' + index + '-part">' +
                    '</td>' +
                    '<td><input type="number" name="bom-' + index + '-quantity" value="1" min="1" class="form-control" id="id_bom-' + index + '-quantity"></td>' +
                    '<td><select name="bom-' + index + '-color_part" class="form-select color-part-select" id="id_bom-' + index + '-color_part">' +
                        '<option value="" selected="">---------</option>' +
                        '<option value="بدنه">بدنه</option>' +
                        '<option value="در">در</option>' +
                        '<option value="دستگیره">دستگیره</option>' +
                        '<option value="پایه">پایه</option>' +
                        '<option value="صفحه">صفحه</option>' +
                    '</select></td>' +
                    '<td class="size-rule-cell">' +
                        '<input type="hidden" name="bom-' + index + '-size_adjustment_rule" class="size-rule-hidden" id="id_bom-' + index + '-size_adjustment_rule">' +
                        '<select class="form-select form-select-sm size-rule-preset">' +
                            '<option value="">--- انتخاب قانون ---</option>' +
                            '<option value="length + length_diff">طول + تغییر طول</option>' +
                            '<option value="length + length_diff/3">(طول + تغییر طول) ÷ ۳</option>' +
                            '<option value="length + length_diff/4">(طول + تغییر طول) ÷ ۴</option>' +
                            '<option value="custom">✏️ سفارشی</option>' +
                        '</select>' +
                        '<input type="text" class="form-control form-control-sm size-rule-custom" style="display:none;" placeholder="فرمول دلخواه">' +
                    '</td>' +
                    '<td class="text-center"></td>' +
                    '<input type="hidden" name="bom-' + index + '-color_material_map" id="id_bom-' + index + '-color_material_map">' +
                '</tr>';
        },

        addRow: function () {
            var self = this;
            var totalForms = $('#id_bom-TOTAL_FORMS');
            var currentCount = parseInt(totalForms.val());
            var tbody = $('#bom-rows');
            var lastRow = tbody.find('tr.bom-row:last');

            var newRow;

            if (lastRow.length === 0) {
                newRow = $(this.buildEmptyRowHtml(currentCount));
                tbody.append(newRow);
                totalForms.val(currentCount + 1);
                this.applyRuleDropdown(newRow[0]);
                return;
            }

            newRow = lastRow.clone(true);

            newRow.find('input,select,textarea').each(function () {
                var name = $(this).attr('name');
                if (name) {
                    $(this).attr('name', name.replace('bom-' + (currentCount - 1) + '-', 'bom-' + currentCount + '-'));
                }
                var id = $(this).attr('id');
                if (id) {
                    $(this).attr('id', id.replace('bom-' + (currentCount - 1) + '-', 'bom-' + currentCount + '-'));
                }
                if ($(this).is('input[type="checkbox"]')) {
                    $(this).prop('checked', false);
                } else if ($(this).is('select')) {
                    $(this).val('');
                } else if (!$(this).is('[type="hidden"]')) {
                    $(this).val('');
                }
            });

            newRow.find('input[name$="-part"]').val('');
            newRow.find('.part-name').text('قطعه‌ای انتخاب نشده');
            newRow.data('part-id', '');
            newRow.find('.edit-part-btn, .add-part-btn').remove();
            var addBtn = '<button type="button" class="btn btn-sm btn-outline-success add-part-btn" title="ایجاد قطعه جدید"><i class="bi bi-plus"></i></button>';
            newRow.find('.part-cell').append(addBtn);
            newRow.find('td:last').html('');
            newRow.find('.size-rule-preset').val('');
            newRow.find('.size-rule-custom').hide().val('');
            newRow.find('.size-rule-hidden').val('');
            newRow.find('input[name$="-color_material_map"]').val('');

            tbody.append(newRow);
            totalForms.val(currentCount + 1);

            this.applyRuleDropdown(newRow[0]);
        },

        loadPartModalFields: function () {
            /* Cache selectors that the modal interacts with, ensuring they exist. */
            this.$table = $('#bom-table');
            this.$modal = $('#partModal');
        },

        // State
        currentRow: null,
        editMode: false,
        editPartId: null,
    };

    // ── Initialize on DOM ready ──
    $(document).ready(function () {
        if ($('#bom-table').length) {
            BomEditor.init();
        }
    });

})(jQuery);
