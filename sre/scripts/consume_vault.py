#!/usr/bin/env python3
"""
Kafka consumer that indexes Obsidian vault changes into Qdrant in real-time.

Replaces the CronJob-based ingest_vault.py for event-driven ingestion.
Reads from the vault-changes topic published by vault_watcher.py on the desktop.
"""

import hashlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

import httpx
from confluent_kafka import Consumer, KafkaError

from sre.config import load

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOPIC = "vault-changes"
COLLECTION = "vault"
GROUP_ID = "vault-ingest-consumer"
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


def embed(http: httpx.Client, ollama_url: str, model: str, texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        r = http.post(
            f"{ollama_url}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=30,
        )
        r.raise_for_status()
        vectors.append(r.json()["embedding"])
    return vectors


def qdrant_delete_file(http: httpx.Client, qdrant_url: str, rel_path: str):
    r = http.post(
        f"{qdrant_url}/collections/{COLLECTION}/points/delete",
        json={"filter": {"must": [{"key": "file", "match": {"value": rel_path}}]}},
        timeout=10,
    )
    r.raise_for_status()


def qdrant_upsert(http: httpx.Client, qdrant_url: str, points: list):
    r = http.put(
        f"{qdrant_url}/collections/{COLLECTION}/points",
        json={"points": points},
        timeout=60,
    )
    r.raise_for_status()


def process_message(http: httpx.Client, cfg: dict, payload: dict):
    action = payload["action"]
    rel_path = payload["path"]
    qdrant_url = cfg["qdrant_url"]
    ollama_url = cfg["embed_ollama_url"]
    model = cfg["embed_model"]

    if action == "deleted":
        qdrant_delete_file(http, qdrant_url, rel_path)
        log.info("deleted: %s", rel_path)
        return

    content = payload.get("content", "")
    if not content.strip():
        log.info("skipped (empty): %s", rel_path)
        return

    chunks = chunk_text(content)
    vectors = embed(http, ollama_url, model, chunks)
    file_hash = hashlib.sha256(content.encode()).hexdigest()

    qdrant_delete_file(http, qdrant_url, rel_path)

    points = [
        {
            "id": abs(hash(f"{rel_path}:{i}")) % (2**53),
            "vector": vec,
            "payload": {"file": rel_path, "chunk": i, "text": chunk, "hash": file_hash},
        }
        for i, (chunk, vec) in enumerate(zip(chunks, vectors))
    ]
    qdrant_upsert(http, qdrant_url, points)
    log.info("%s: %s (%d chunks)", action, rel_path, len(chunks))


def main():
    cfg = load()
    bootstrap = cfg.get("redpanda_url", os.environ.get("REDPANDA_URL", "localhost:9092"))

    consumer = Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": GROUP_ID,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([TOPIC])
    log.info("Subscribed to %s on %s", TOPIC, bootstrap)

    running = True

    def shutdown(sig, frame):
        nonlocal running
        log.info("Shutting down")
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    with httpx.Client() as http:
        while running:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("Kafka error: %s", msg.error())
                continue
            try:
                payload = json.loads(msg.value().decode())
                process_message(http, cfg, payload)
                consumer.commit(message=msg)
            except Exception as e:
                log.error("Failed to process %s: %s", msg.key(), e)

    consumer.close()


if __name__ == "__main__":
    main()
