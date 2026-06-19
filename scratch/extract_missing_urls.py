import re

log_file = "local_scraper.log"
output_file = "missing_urls.txt"
base_url = "https://www.hornbach.de"

missing_paths = set()

with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if "Local file not found for:" in line:
            # Extract the path part after the colon
            match = re.search(r'Local file not found for:\s*(.*)', line)
            if match:
                path = match.group(1).strip()
                
                # Normalize path: find where /p/ or /conf/ starts
                m = re.search(r'/(p|conf)/', path)
                if m:
                    normalized_path = path[m.start():].split('?')[0].rstrip('/')
                    # Ensure it doesn't end with .index.html if we want the clean URL
                    # but actually Hornbach URLs usually end with the number slash
                    missing_paths.add(base_url + normalized_path + "/")

with open(output_file, 'w', encoding='utf-8') as f:
    for url in sorted(list(missing_paths)):
        f.write(url + "\n")

print(f"Extraction complete. {len(missing_paths)} unique missing URLs saved to {output_file}")
