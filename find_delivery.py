with open('inventory/services.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find execute_daily_delivery
start_idx = None
end_idx = None
for i, line in enumerate(lines):
    if 'def execute_daily_delivery' in line:
        start_idx = i
    if start_idx is not None and i > start_idx and line.strip() == 'return queue' and 'return queue' in lines[i+1]:
        # Find the end of the function - look for the next function definition
        for j in range(i+1, len(lines)):
            if lines[j].startswith('def ') or lines[j].startswith('@'):
                end_idx = j
                break
        break

if start_idx is None or end_idx is None:
    print("Could not find function boundaries")
else:
    print(f"Found execute_daily_delivery at lines {start_idx+1}-{end_idx}")
    print(lines[start_idx])
    print(lines[end_idx-1])