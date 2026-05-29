#!/usr/bin/env python3
"""
Watches the Obsidian vault for file changes and publishes events to Redpanda.

Runs as a systemd service on the desktop. Each create/modify/delete publishes
a JSON message to the vault-changes topic.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from confluent_kafka import Producer
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from sre.config import load

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOPIC = "vault-changes"


def make_producer(bootstrap_servers: str) -> Producer:
    return Producer({"bootstrap.servers": bootstrap_servers, "client.id": "vault-watcher"})


def delivery_report(err, msg):
    if err:
        log.error("Delivery failed for %s: %s", msg.key(), err)
    else:
        log.debug("Delivered %s to %s [%d]", msg.key(), msg.topic(), msg.partition())


class VaultEventHandler(FileSystemEventHandler):
    def __init__(self, producer: Producer, vault_path: Path):
        self.producer = producer
        self.vault_path = vault_path

    def _publish(self, action: str, path: str):
        rel_path = str(Path(path).relative_to(self.vault_path))
        if not rel_path.endswith(".md"):
            return

        payload: dict = {"path": rel_path, "action": action}

        if action in ("created", "modified"):
            try:
                payload["content"] = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                log.warning("Could not read %s: %s", path, e)
                return

        self.producer.produce(
            TOPIC,
            key=rel_path.encode(),
            value=json.dumps(payload).encode(),
            callback=delivery_report,
        )
        self.producer.poll(0)
        log.info("%s: %s", action, rel_path)

    def on_created(self, event):
        if not event.is_directory:
            self._publish("created", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._publish("modified", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._publish("deleted", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._publish("deleted", event.src_path)
            self._publish("created", event.dest_path)


def main():
    cfg = load()
    vault_path = Path(cfg.get("vault_path", str(Path.home() / "Documents/obsidian-vault")))
    bootstrap = cfg.get("redpanda_url", os.environ.get("REDPANDA_URL", "localhost:9092"))

    if not vault_path.exists():
        log.error("Vault path does not exist: %s", vault_path)
        sys.exit(1)

    log.info("Watching %s → topic %s on %s", vault_path, TOPIC, bootstrap)

    producer = make_producer(bootstrap)
    handler = VaultEventHandler(producer, vault_path)
    observer = Observer()
    observer.schedule(handler, str(vault_path), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
            producer.poll(0)
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        observer.stop()
        observer.join()
        producer.flush()


if __name__ == "__main__":
    main()
