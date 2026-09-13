with open('product/forms.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Replace lines 542-553 (0-indexed: 541-552)
new_lines = []
i = 0
while i < len(lines):
    if i == 542:  # fields line
        new_lines.append("        fields = ['process', 'product', 'color_part', 'raw_material', 'consumption_per_unit']\n")
    elif i == 545:  # 'process': line in widgets
        new_lines.append(lines[i])  # keep process line
        new_lines.append("            'product': forms.Select(attrs={'class': 'form-select'}),\n")
        new_lines.append("            'color_part': forms.Select(attrs={'class': 'form-select'}),\n")
    elif i == 549:  # 'process': line in labels
        new_lines.append(lines[i])  # keep process line
        new_lines.append("            'product': '\u0645\u062d\u0635\u0648\u0644',\n")
        new_lines.append("            'color_part': '\u0628\u062e\u0634 \u0631\u0646\u06af\u06cc',\n")
    elif i == 542 or i == 545 or i == 549:
        pass  # handled above
    else:
        new_lines.append(lines[i])
    i += 1

with open('product/forms.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Done')