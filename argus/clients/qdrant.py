import hashlib
import httpx

COLLECTION = "vault"
CHUNK_SIZE = 500


def chunk_text(text: str) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if len(current) + len(para) > CHUNK_SIZE and current:
            chunks.append(current.strip())
            current = para
        else:
            current = (current + "\n\n" + para).strip()
    if current:
        chunks.append(current)
    return chunks or [text[:CHUNK_SIZE]]


def upsert_file(qdrant_url: str, ollama_url: str, model: str, rel_path: str, content: str):
    """Chunk, embed, and upsert a file directly into Qdrant. Replaces any existing points."""
    chunks = chunk_text(content)
    file_hash = hashlib.sha256(content.encode()).hexdigest()

    with httpx.Client(verify=False, timeout=60) as http:
        vectors = []
        for chunk in chunks:
            r = http.post(
                f"{ollama_url}/api/embeddings",
                json={"model": model, "prompt": chunk},
                timeout=30,
            )
            r.raise_for_status()
            vectors.append(r.json()["embedding"])

        http.post(
            f"{qdrant_url}/collections/{COLLECTION}/points/delete",
            json={"filter": {"must": [{"key": "file", "match": {"value": rel_path}}]}},
            timeout=10,
        ).raise_for_status()

        points = [
            {
                "id": int(hashlib.md5(f"{rel_path}:{i}".encode()).hexdigest(), 16) % (2**63),
                "vector": vec,
                "payload": {"file": rel_path, "chunk": i, "text": chunk, "hash": file_hash},
            }
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        http.put(
            f"{qdrant_url}/collections/{COLLECTION}/points",
            json={"points": points},
            timeout=60,
        ).raise_for_status()
