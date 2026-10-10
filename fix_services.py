with open('inventory/services.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Replace lines 104-147 (0-indexed: 103-146)
new_lines = lines[:103]  # up to line 103 (the blank line after _packs_counted)

new_code = '''
def handout_amount(stock, pack):
    """
    قانون تحویل: "قوطی باز اول، بعد بستهٔ سربسته".

    - اگر pack <= 0 (بدون بسته‌بندی): None برمی‌گرداند تا انباردار دستی وارد کند.
    - open_rem = stock % pack  -> باقی‌مانده قوطی باز
    - اگر open_rem > 0: تحویل open_rem (قوطی باز)
    - وگرنه: تحویل min(pack, stock) (یک بسته کامل، یا همهٔ موجودی اگر کمتر از بسته)
    """
    stock = _q2(stock or 0)
    pack = _q2(pack or 0)
    if stock <= 0:
        return ZERO
    if pack <= 0:
        return None  # انباردار دستی وارد کند
    open_rem = stock % pack
    if open_rem > 0:
        return open_rem
    return min(pack, stock)


'''

new_lines.extend(new_code.splitlines(keepends=True))
new_lines.extend(lines[147:])  # from 'def receive_quantity' onwards

with open('inventory/services.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Done')