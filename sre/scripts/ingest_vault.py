#!/usr/bin/env python3
"""Ingest Obsidian vault markdown files into Qdrant for semantic search."""

import hashlib
import re
from pathlib import Path

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from sre import config as cfg_module

COLLECTION = "vault"
CHUNK_SIZE = 400
CHUNK_OVERLAP = 50


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + size])
        if chunk.strip():
            chunks.append(chunk)
        i += size - overlap
    return chunks


def embed(text: str, ollama_url: str) -> list[float]:
    resp = httpx.post(
        f"{ollama_url}/api/embeddings",
        json={"model": "nomic-embed-text", "prompt": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def main():
    cfg = cfg_module.load()
    vault_path = Path(cfg["vault_path"])
    ollama_url = cfg.get("embed_ollama_url", "http://10.0.0.113:11434")
    qdrant_url = cfg.get("qdrant_url", "https://qdrant.home.t12.io")

    client = QdrantClient(url=qdrant_url, verify=False)

    sample_vec = embed("test", ollama_url)
    dim = len(sample_vec)

    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        print(f"Created collection '{COLLECTION}' (dim={dim})")
    else:
        print(f"Collection '{COLLECTION}' exists")

    md_files = list(vault_path.rglob("*.md"))
    print(f"Found {len(md_files)} markdown files")

    BATCH_SIZE = 50
    points = []
    total_chunks = 0

    def flush(batch):
        if batch:
            client.upsert(collection_name=COLLECTION, points=batch)

    for path in md_files:
        text = path.read_text(errors="ignore")
        text = re.sub(r"^---.*?---\n", "", text, flags=re.DOTALL)
        text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)

        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            uid = hashlib.md5(f"{path}:{i}".encode()).hexdigest()
            uid_int = int(uid, 16) % (2**63)
            vec = embed(chunk, ollama_url)
            points.append(PointStruct(
                id=uid_int,
                vector=vec,
                payload={
                    "file": str(path.relative_to(vault_path)),
                    "chunk": i,
                    "text": chunk,
                },
            ))
            total_chunks += 1
            print(f"  {path.name} chunk {i}")
            if len(points) >= BATCH_SIZE:
                flush(points)
                points = []

    flush(points)
    print(f"\nIngested {total_chunks} chunks from {len(md_files)} files")


if __name__ == "__main__":
    main()
