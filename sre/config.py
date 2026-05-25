import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "prometheus_url": "https://prometheus.home.t12.io",
    "alertmanager_url": "https://alertmanager.home.t12.io",
    "loki_url": "http://loki.home.t12.io",
    "ollama_url": "https://litellm.home.t12.io",
    "ollama_model": "mistral",
    "ollama_api_key": "",
    "vault_path": str(Path.home() / "Documents/obsidian-vault"),
    "qdrant_url": "https://qdrant.home.t12.io",
    "embed_ollama_url": "http://10.0.0.113:11434",
    "embed_model": "nomic-embed-text",
}

def load() -> dict:
    cfg = dict(DEFAULTS)
    config_file = Path.home() / ".config" / "sre" / "config.toml"
    if config_file.exists():
        with open(config_file, "rb") as f:
            cfg.update(tomllib.load(f))
    for key in DEFAULTS:
        env_key = f"SRE_{key.upper()}"
        if val := os.environ.get(env_key):
            cfg[key] = val
    return cfg
