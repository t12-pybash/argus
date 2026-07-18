import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "prometheus_url": "https://prometheus.your-cluster.example",
    "alertmanager_url": "https://alertmanager.your-cluster.example",
    "loki_url": "https://loki.your-cluster.example",
    "ollama_url": "https://litellm.your-cluster.example",
    "ollama_model": "llama3.1:8b",
    "diagnose_model": "llama3.1:8b",
    "ollama_api_key": "",
    "vault_path": str(Path.home() / "Documents/obsidian-vault"),
    "qdrant_url": "https://qdrant.your-cluster.example",
    "embed_ollama_url": "http://localhost:11434",
    "embed_model": "nomic-embed-text",
    "redpanda_url": "redpanda.messaging.svc.cluster.local:9092",
    "agent_webhook_url": "",
    "agent_webhook_type": "",
    "agent_poll_interval": "30",
    "agent_approval_timeout": "300",
    "agent_webhook_port": "7654",
    "agent_interactivity_url": "",
    "slack_bot_token": "",
    "slack_app_token": "",
    "slack_channel": "#homelab-alerts",
    "gitlab_url": "https://gitlab.com",
    "gitlab_token": "",
    "gitlab_project_id": "",
    "gitlab_target_branch": "main",
}

def load() -> dict:
    cfg = dict(DEFAULTS)
    config_file = Path(os.environ.get("ARGUS_CONFIG_FILE", str(Path.home() / ".config" / "argus" / "config.toml")))
    if config_file.exists():
        with open(config_file, "rb") as f:
            cfg.update(tomllib.load(f))
    for key in DEFAULTS:
        env_key = f"ARGUS_{key.upper()}"
        if val := os.environ.get(env_key):
            cfg[key] = val
    return cfg
