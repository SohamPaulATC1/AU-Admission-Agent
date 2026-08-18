import bisect
import json

with open("senco_llm2.json", "r", encoding="utf-8") as f:
    stores_data = json.load(f)

# Sort once
stores_data.sort(key=lambda x: x["pincode"])

# Extract pincodes
pincodes = [store["pincode"] for store in stores_data]


def find_stores(user_pincode):
    
    result = []
    
    # 🔍 Binary search position
    pos = bisect.bisect_left(pincodes, user_pincode)
    
    left = pos - 1
    right = pos
    
    # ⚡ First: collect exact matches
    while right < len(stores_data) and pincodes[right] == user_pincode:
        result.append(stores_data[right])
        right += 1
    
    # (Optional) also check left side for duplicates
    while left >= 0 and pincodes[left] == user_pincode:
        result.append(stores_data[left])
        left -= 1
    
    # 🔥 Then: expand to get remaining closest
    while len(result) < 5 and (left >= 0 or right < len(stores_data)):
        
        if left < 0:
            result.append(stores_data[right])
            right += 1
        elif right >= len(stores_data):
            result.append(stores_data[left])
            left -= 1
        else:
            if abs(pincodes[left] - user_pincode) <= abs(pincodes[right] - user_pincode):
                result.append(stores_data[left])
                left -= 1
            else:
                result.append(stores_data[right])
                right += 1
    
    return result[:5]  # ensure max 5

print(find_stores(700014))