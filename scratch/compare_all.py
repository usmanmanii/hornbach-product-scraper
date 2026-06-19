import csv
import os
import re
from pathlib import Path

csv_info_path = Path("product-info.csv")
output_dir = Path("output_local/specs")

def slugify(name: str) -> str:
    umlaut_map = {
        "ä": "ae", "ö": "oe", "ü": "ue",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
    }
    for char, replacement in umlaut_map.items():
        name = name.replace(char, replacement)
    name = name.lower()
    name = re.sub(r"[&,\s]+", "_", name)
    name = re.sub(r"[^\w_]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name

categories = []
with open(csv_info_path, newline="", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    header_found = False
    for row in reader:
        if not row or len(row) < 3:
            continue
        if not header_found:
            if row[0].strip().lower() == "name":
                header_found = True
            continue
        try:
            name = row[0].strip()
            # Handle cases where count might have commas or be in a different column
            count_str = row[2].strip()
            if not count_str.isdigit():
                # Check column 4 if column 3 is URL (row 6 case)
                if "http" in row[1] and row[3].strip().isdigit():
                    count_str = row[3].strip()
            
            expected = int(count_str)
            categories.append({"name": name, "expected": expected})
        except:
            continue

print(f"{'Category':<40} | {'Expected':>10} | {'Fetched':>10} | {'Remaining':>10} | {'% Done':>8} | {'% Left':>8}")
print("-" * 100)

total_expected = 0
total_fetched = 0

for cat in categories:
    slug = slugify(cat['name'])
    output_file = output_dir / f"{slug}.csv"
    fetched = 0
    if output_file.exists():
        with open(output_file, 'r', encoding='utf-8') as f:
            # Count lines - 1 for header
            fetched = max(0, sum(1 for line in f) - 1)
    
    remaining = max(0, cat['expected'] - fetched)
    done_pct = (fetched / cat['expected'] * 100) if cat['expected'] > 0 else 0
    left_pct = (remaining / cat['expected'] * 100) if cat['expected'] > 0 else 0
    print(f"{cat['name']:<40} | {cat['expected']:>10} | {fetched:>10} | {remaining:>10} | {done_pct:>7.2f}% | {left_pct:>7.2f}%")
    
    total_expected += cat['expected']
    total_fetched += fetched

total_remaining = max(0, total_expected - total_fetched)
print("-" * 100)
total_done_pct = (total_fetched / total_expected * 100) if total_expected > 0 else 0
total_left_pct = (total_remaining / total_expected * 100) if total_expected > 0 else 0
print(f"{'TOTAL':<40} | {total_expected:>10} | {total_fetched:>10} | {total_remaining:>10} | {total_done_pct:>7.2f}% | {total_left_pct:>7.2f}%")
