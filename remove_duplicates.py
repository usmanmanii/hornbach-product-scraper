import csv
import sys
import os

def remove_duplicates(csv_path):
    if not os.path.exists(csv_path):
        print(f"Error: File not found - {csv_path}")
        sys.exit(1)
        
    seen = set()
    unique_rows = []
    fieldnames = []
    
    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            print(f"Error: Could not read headers from {csv_path}. Is it empty?")
            sys.exit(1)
            
        fieldnames = reader.fieldnames
        for row in reader:
            art_num = row.get('article_number')
            url = row.get('product_url')
            # Use article_number as unique identifier, fallback to URL
            uid = art_num if (art_num and art_num.strip() != "") else url
            
            if uid not in seen:
                seen.add(uid)
                unique_rows.append(row)
                
    # Calculate original total count for reporting
    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        total_rows = max(0, sum(1 for _ in f) - 1)  # Subtract 1 for header

    # Overwrite the same file with unique rows
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(unique_rows)
        
    print(f"Successfully processed: {csv_path}")
    print(f"Original rows         : {total_rows}")
    print(f"Unique rows remaining : {len(unique_rows)}")
    print(f"Duplicates removed    : {total_rows - len(unique_rows)}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python remove_duplicates.py <path_to_csv>")
        sys.exit(1)
        
    remove_duplicates(sys.argv[1])
