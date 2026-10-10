with open('inventory/services.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Replace lines 1090-1180 (0-indexed: 1089-1179)
new_lines = lines[:1089]  # up to line 1089 (the @transaction.atomic line)

new_code = '''@transaction.atomic
def execute_daily_delivery(*, queue_id, delivered_by, note='', quantity=None):
    """
    تحویل مواد برای یک ردیف صف — تنها نقطهٔ کسر موجودی بابت تولید.

    quantity=None → قانون قوطی باز/بسته کامل (handout_amount)
    quantity=X    → انباردار مقدار دلخواه را تحویل می‌دهد.

    اکنون در DailyMaterialHandout ثبت می‌شود. صف نیاز (DailyMaterialQueue)
    فقط برنامه‌ریزی را نگه می‌دارد.
    """
    try:
        queue = DailyMaterialQueue.objects.select_for_update().get(pk=queue_id)
    except DailyMaterialQueue.DoesNotExist:
        raise HandoverError('ردیف صف مواد روزانه یافت نشد.')

    _assert_date_not_locked(queue.work_date)

    if queue.status == 'cancelled':
        raise HandoverError(
            'این ردیف از برنامهٔ روز حذف شده است؛ تا زمانی که نیاز آن در '
            'برنامه نباشد، تحویل ممکن نیست.'
        )

    # Get or create handout record
    handout, _ = DailyMaterialHandout.objects.select_for_update().get_or_create(
        work_date=queue.work_date,
        worker=queue.worker,
        raw_material=queue.raw_material,
        defaults={'delivered_quantity': ZERO, 'weighed_remaining': None},
    )

    # Lock raw_material row to prevent race condition on stock check/update
    raw_material = RawMaterial.objects.select_for_update().get(pk=queue.raw_material_id)
    pack = _q2(raw_material.pack_size or 0)
    stock = _q2(raw_material.current_stock)

    if quantity is None:
        physical = handout_amount(stock, pack)
        if physical is None:
            raise HandoverError(
                f'برای «{raw_material.name}» اندازهٔ بسته تعریف نشده است. '
                'مقدار تحویل را صریحاً وارد کنید.'
            )
    else:
        try:
            physical = _q2(quantity)
        except (TypeError, ValueError, ArithmeticError, InvalidOperation):
            raise HandoverError('مقدار تحویل معتبر نیست.')
        if not physical.is_finite() or physical <= 0:
            raise HandoverError('مقدار تحویل باید بزرگ‌تر از صفر باشد.')

    if physical > stock:
        raise HandoverError(
            f'موجودی انبار کافی نیست. برای «{raw_material.name}» '
            f'مقدار تحویل {physical} لازم است اما موجودی {stock} است.'
        )

    # Get primary task and order_item for traceability
    ref_task, ref_order_item = _get_primary_task_and_order(queue)

    StockMovement.objects.create(
        raw_material=raw_material,
        movement_type='consumption',
        quantity=physical,
        daily_queue=queue,
        handout=handout,
        reference_task=ref_task,
        reference_order_item=ref_order_item,
        created_by=delivered_by,
        note=(note or '')[:255] or f'تحویل روزانه — {raw_material.name} به {queue.worker}',
    )
    handout.delivered_quantity = _q2(handout.delivered_quantity + physical)
    handout.weighed_remaining = None  # Reset weighed status on new delivery
    handout.save(update_fields=['delivered_quantity', 'weighed_remaining', 'updated_at'])

    next_date = _shift_working_date(queue.work_date, +1)
    if next_date is not None:
        transaction.on_commit(lambda d=next_date: request_queue_sync([d]))
    return handout


'''

new_lines.extend(new_code.splitlines(keepends=True))
new_lines.extend(lines[1180:])  # from line 1180 onwards (_mark_rework_defects_delivered)

with open('inventory/services.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Done')