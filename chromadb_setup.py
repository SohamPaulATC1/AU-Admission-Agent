import json
import chromadb

db_path = "./senco_chroma_db"

chroma_client = chromadb.PersistentClient(path=db_path)

# Reset collection
try:
    chroma_client.delete_collection(name="senco_stores")
except ValueError:
    pass

collection = chroma_client.get_or_create_collection(name="senco_stores")

with open("senco_llm4.json", "r", encoding="utf-8") as f:
    raw_data = json.load(f)

documents = []
metadatas = []
ids = []

for i, store in enumerate(raw_data):

    address = store.get("address", "")
    pincode = str(store.get("pincode", ""))
    metadata = store.get("metadata", {})

    # --- Build searchable document string ---
    metadata_pieces = []
    for key, value in metadata.items():
        if key == "phones":
            metadata_pieces.append(f"phones: {', '.join(value)}")
        else:
            metadata_pieces.append(f"{key.replace('_', ' ')}: {value}")

    metadata_pieces.append(f"pincode: {pincode}")
    metadata_string = ", ".join(metadata_pieces)

    combined_search_string = f"{address} Details: {metadata_string}"
    documents.append(combined_search_string)

    # --- Build ChromaDB metadata (flat, string values only) ---
    enriched_metadata = {
        "state": metadata.get("state", ""),
        "district": metadata.get("district", ""),
        "store_name": metadata.get("store_name", ""),
        "phones": ", ".join(metadata.get("phones", [])),  # array -> comma string
        "pincode": pincode
    }
    metadatas.append(enriched_metadata)

    ids.append(f"store_{i}")

collection.upsert(
    documents=documents,
    metadatas=metadatas,
    ids=ids
)

print(f"Successfully loaded {len(documents)} stores into ChromaDB!")