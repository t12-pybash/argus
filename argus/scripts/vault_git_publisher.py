#!/usr/bin/env python3
"""
Git-based vault publisher. Runs as a CronJob every 10 minutes.

Pulls the Obsidian vault from GitHub, diffs against the last processed commit,
and publishes changed .md files to Redpanda. The vault-ingest-consumer handles
chunk → embed → upsert into Qdrant from there.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

from confluent_kafka import Producer

from argus.config import load

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOPIC = "vault-changes"
VAULT_REPO = "git@github.com:t12-pybash/obsidian-vault.git"
VAULT_DIR = Path("/vault")
SHA_FILE = Path.home() / ".config" / "argus" / "last_sha"


def setup_ssh():
    ssh_dir = Path("/root/.ssh")
    ssh_dir.mkdir(mode=0o700, exist_ok=True)

    key_src = Path("/ssh/id_ed25519")
    if key_src.exists():
        dest = ssh_dir / "id_ed25519"
        dest.write_bytes(key_src.read_bytes())
        dest.chmod(0o600)

    result = subprocess.run(["ssh-keyscan", "-H", "github.com"], capture_output=True, text=True)
    with open(ssh_dir / "known_hosts", "a") as f:
        f.write(result.stdout)

    # PVC may be owned by a different UID than the pod — mark it safe
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(VAULT_DIR)], check=True)


def git(*args, cwd=None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd or VAULT_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def sync_repo() -> str:
    """Clone or pull to latest. Returns HEAD SHA."""
    if (VAULT_DIR / ".git").exists():
        git("fetch", "origin")
        git("reset", "--hard", "origin/main")
    else:
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", VAULT_REPO, str(VAULT_DIR)],
            check=True, capture_output=True, text=True,
        )
    return git("rev-parse", "HEAD")


def changed_since(from_sha: str | None) -> tuple[list[str], list[str]]:
    """Returns (modified_paths, deleted_paths) relative to VAULT_DIR."""
    if from_sha is None:
        all_md = [str(p.relative_to(VAULT_DIR)) for p in VAULT_DIR.rglob("*.md")]
        return all_md, []

    output = git("diff", "--name-status", f"{from_sha}..HEAD")
    modified, deleted = [], []
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2 or not parts[1].endswith(".md"):
            continue
        status, path = parts
        (deleted if status.startswith("D") else modified).append(path)

    return modified, deleted


def publish(producer: Producer, rel_path: str, action: str):
    payload: dict = {"path": rel_path, "action": action}
    if action != "deleted":
        full = VAULT_DIR / rel_path
        try:
            payload["content"] = full.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            log.warning("Could not read %s: %s", rel_path, e)
            return
    producer.produce(TOPIC, key=rel_path.encode(), value=json.dumps(payload).encode())


def main():
    setup_ssh()
    head_sha = sync_repo()

    last_sha = SHA_FILE.read_text().strip() if SHA_FILE.exists() else None

    if last_sha == head_sha:
        log.info("No changes since last run (%s)", head_sha[:8])
        return

    log.info("Diffing %s → %s", (last_sha or "initial")[:8], head_sha[:8])
    modified, deleted = changed_since(last_sha)

    if not modified and not deleted:
        log.info("No .md files changed")
    else:
        cfg = load()
        bootstrap = cfg.get("redpanda_url", os.environ.get("REDPANDA_URL", "localhost:9092"))
        producer = Producer({"bootstrap.servers": bootstrap, "client.id": "vault-git-publisher"})

        for rel_path in deleted:
            publish(producer, rel_path, "deleted")
            log.info("deleted: %s", rel_path)

        for rel_path in modified:
            publish(producer, rel_path, "modified")
            log.info("modified: %s", rel_path)

        producer.flush()
        log.info("Published — %d modified, %d deleted", len(modified), len(deleted))

    SHA_FILE.parent.mkdir(parents=True, exist_ok=True)
    SHA_FILE.write_text(head_sha)
    log.info("SHA updated to %s", head_sha[:8])


if __name__ == "__main__":
    main()
