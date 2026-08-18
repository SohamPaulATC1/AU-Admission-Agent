import json
import os
import chromadb
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

chroma_client = chromadb.PersistentClient(path="./senco_chroma_db")
collection = chroma_client.get_or_create_collection(name="senco_stores")

user_question = "BowBazar"

results = collection.query(
    query_texts=[user_question],
    n_results=3
)

if results['documents'] and results['documents'][0]:
    context_chunks = []
    for i in range(len(results['documents'][0])):
        doc_text = results['documents'][0][i]
        metadata = results['metadatas'][0][i]
        
        chunk = f"Store Description: {doc_text}\nStore Metadata (includes phone numbers): {metadata}"
        context_chunks.append(chunk)
        
    retrieved_context = "\n\n".join(context_chunks)
else:
    retrieved_context = "No data found in database."

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

completion = client.chat.completions.create(
    model="meta-llama/llama-4-scout-17b-16e-instruct",
    messages=[
      {
        "role": "system",
        "content": (
            "You are a helpful assistant. Answer the user's question using ONLY the provided context. "
            "If the answer is not contained in the context, say 'I do not have enough information to answer that.'\n\n"
            f"CONTEXT:\n{retrieved_context}"
        )
      },
      {
        "role": "user",
        "content": user_question
      }
    ],
    temperature=0.1,
    max_completion_tokens=1024,
    stream=True
)

print("\n--- LLM Response ---\n")
for chunk in completion:
    print(chunk.choices[0].delta.content or "", end="")