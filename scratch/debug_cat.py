import json
import re
from bs4 import BeautifulSoup

def extract_category_from_apollo(html: str) -> str:
    print("Testing extraction...")
    try:
        # Search for the Apollo state JSON
        match = re.search(r'window\.__ARTICLE_DETAIL_APOLLO_STATE__\s*=\s*(.*?);\s*</script>', html, re.DOTALL)
        if not match:
            print("Regex 1 failed")
            match = re.search(r'__ARTICLE_DETAIL_APOLLO_STATE__\s*=\s*({.*?})', html, re.DOTALL)
            
        if not match:
            print("Regex 2 failed")
            return "Uncategorized"

        state_data = match.group(1).strip()
        print(f"Captured state data length: {len(state_data)}")
        state = json.loads(state_data)
        
        sortiment_id = "R000000"
        breadcrumbs = []
        for key, val in state.items():
            if isinstance(val, dict) and "__typename" in val and val["__typename"] == "Breadcrumb":
                breadcrumbs.append(val)
        
        print(f"Found {len(breadcrumbs)} breadcrumbs")
        cats = [b for b in breadcrumbs if b.get("id") != sortiment_id and b.get("name") and b.get("name").lower() != "sortiment"]
        if cats:
            cats.sort(key=lambda x: len(x.get("url", "")))
            print(f"Top cat: {cats[0].get('name')}")
            return cats[0].get("name", "Uncategorized")
            
    except Exception as e:
        print(f"Error: {e}")
    
    return "Uncategorized"

# Read one of the downloaded files
file_path = "www.hornbach.de/conf/akustikpaneel-konfigurator-digital-bedruckt-dein-individuelles-motiv/12571103/index.html.tmp"
with open(file_path, "r", encoding="utf-8") as f:
    html = f.read()

print(f"Result: {extract_category_from_apollo(html)}")
