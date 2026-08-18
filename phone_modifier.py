import json
import re

with open("senco_llm3.json", "r", encoding="utf-8") as f:
    data = json.load(f)

for store in data:
    metadata = store.get("metadata", {})
    phones = []
    
    # Collect all phoneN keys in order
    i = 1
    while f"phone{i}" in metadata:
        phones.append(metadata.pop(f"phone{i}"))
        i += 1
    
    metadata["phones"] = phones

with open("senco_llm4.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=4, ensure_ascii=False)

print(f"Done! {len(data)} records processed.")