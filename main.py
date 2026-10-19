import os
import requests
from dotenv import load_dotenv

load_dotenv()

rag_api_token = os.getenv("RAG_API_TOKEN")
if not rag_api_token:
    raise RuntimeError(
        "RAG_API_TOKEN is not set. Add it to a .env file or export it in your shell."
    )

response = requests.post(
    "http://127.0.0.1:8001/api/v1/rag/ask",
    headers={
        "Authorization": f"Bearer {rag_api_token}",
        "Accept": "application/json",
    },
    json={
        "question": "How do while loops work?",
        "limit": 5,
    },
    timeout=300,
)

if not response.ok:
    print(response.status_code, response.url, response.text[:1000])

response.raise_for_status()
result = response.json()

print(result["answer"])
print("Sources:", result["sources"])