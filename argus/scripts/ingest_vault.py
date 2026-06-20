#!/usr/bin/env python3
"""Ingest Obsidian vault markdown files into Qdrant for semantic search."""

import hashlib
import json
import re
from pathlib import Path

import httpx

from argus import config as cfg_module

COLLECTION = "vault"
CHUNK_SIZE = 400
CHUNK_OVERLAP = 50
CACHE_PATH = Path.home() / ".config" / "sre" / "ingest_cache.json"


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


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def qdrant_collections(client: httpx.Client, qdrant_url: str) -> list[str]:
    r = client.get(f"{qdrant_url}/collections", timeout=10)
    r.raise_for_status()
    return [c["name"] for c in r.json()["result"]["collections"]]


def qdrant_create_collection(client: httpx.Client, qdrant_url: str, dim: int) -> None:
    r = client.put(
        f"{qdrant_url}/collections/{COLLECTION}",
        json={"vectors": {"size": dim, "distance": "Cosine"}},
        timeout=10,
    )
    r.raise_for_status()


def qdrant_delete_file(client: httpx.Client, qdrant_url: str, rel_path: str) -> None:
    r = client.post(
        f"{qdrant_url}/collections/{COLLECTION}/points/delete",
        json={"filter": {"must": [{"key": "file", "match": {"value": rel_path}}]}},
        timeout=10,
    )
    r.raise_for_status()


def qdrant_upsert(client: httpx.Client, qdrant_url: str, points: list) -> None:
    r = client.put(
        f"{qdrant_url}/collections/{COLLECTION}/points",
        json={"points": points},
        timeout=60,
    )
    r.raise_for_status()


def main():
    cfg = cfg_module.load()
    vault_path = Path(cfg["vault_path"])
    ollama_url = cfg.get("embed_ollama_url", "http://localhost:11434")
    qdrant_url = cfg.get("qdrant_url", "https://qdrant.your-cluster.example")

    http = httpx.Client(verify=False)

    sample_vec = embed("test", ollama_url)
    dim = len(sample_vec)

    existing = qdrant_collections(http, qdrant_url)
    if COLLECTION not in existing:
        qdrant_create_collection(http, qdrant_url, dim)
        print(f"Created collection '{COLLECTION}' (dim={dim})")
    else:
        print(f"Collection '{COLLECTION}' exists")

    cache = load_cache()
    md_files = list(vault_path.rglob("*.md"))
    current_files = {str(p.relative_to(vault_path)) for p in md_files}
    print(f"Found {len(md_files)} markdown files")

    removed = [f for f in cache if f not in current_files]
    for rel_path in removed:
        qdrant_delete_file(http, qdrant_url, rel_path)
        del cache[rel_path]
        print(f"  Removed (deleted file): {rel_path}")

    BATCH_SIZE = 50
    points = []
    total_chunks = 0
    skipped = 0
    changed = 0

    for path in md_files:
        rel_path = str(path.relative_to(vault_path))
        current_hash = file_hash(path)

        if cache.get(rel_path) == current_hash:
            skipped += 1
            continue

        if rel_path in cache:
            qdrant_delete_file(http, qdrant_url, rel_path)

        text = path.read_text(errors="ignore")
        text = re.sub(r"^---.*?---\n", "", text, flags=re.DOTALL)
        text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)

        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            uid = hashlib.md5(f"{rel_path}:{i}".encode()).hexdigest()
            uid_int = int(uid, 16) % (2**63)
            vec = embed(chunk, ollama_url)
            points.append({
                "id": uid_int,
                "vector": vec,
                "payload": {"file": rel_path, "chunk": i, "text": chunk},
            })
            total_chunks += 1
            if len(points) >= BATCH_SIZE:
                qdrant_upsert(http, qdrant_url, points)
                points = []

        cache[rel_path] = current_hash
        changed += 1
        print(f"  Indexed: {path.name} ({len(chunks)} chunks)")

    if points:
        qdrant_upsert(http, qdrant_url, points)
    save_cache(cache)
    http.close()
    print(f"\nDone — {changed} changed, {skipped} skipped, {len(removed)} removed, {total_chunks} chunks written")


if __name__ == "__main__":
    main()
