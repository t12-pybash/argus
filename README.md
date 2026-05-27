# sre-cli

A command-line tool and MCP server for operating a self-hosted Kubernetes homelab with a private AI platform. Combines live cluster observability (Prometheus, Alertmanager) with semantic search over a local knowledge base (Qdrant + Ollama embeddings) and an incident lifecycle manager.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                      sre-cli                        │
│                                                     │
│  Commands          Clients         MCP Server       │
│  ─────────         ───────         ──────────       │
│  sre status   ──▶  Prometheus  ──▶ get_cluster_     │
│  sre alert    ──▶  Alertmanager    status()         │
│  sre runbook  ──▶  Qdrant      ──▶ get_alerts()     │
│  sre ask      ──▶  Ollama/     ──▶ prometheus_      │
│  sre incident      LiteLLM         query()          │
│                                ──▶ search_vault()   │
└─────────────────────────────────────────────────────┘
         │                                │
         ▼                                ▼
  Terminal output                  Claude Code IDE
  (rich formatting)                (AI assistant with
                                    live cluster context)
```

## Commands

```
sre status                        Node health, firing alerts, high-restart pods
sre alert                         Active Alertmanager alerts
sre runbook <query>               Semantic search over runbooks and vault notes
sre runbook <query> --ask         Same, with LLM synthesis of results
sre ask "<question>"              Ask the LLM about the cluster with live Prometheus context
sre incident start "<title>"      Open an incident, snapshot current cluster state
sre incident note "<message>"     Append a timestamped observation
sre incident close "<summary>"    Close the incident with resolution
sre incident show                 Show the active incident
```

### Example output

```
$ sre alert
● CPUThrottlingHigh  [warning]  ns=monitoring
● Watchdog           [none]     ns=monitoring

$ sre runbook "etcd quorum loss"
## Vault search (semantic): 'etcd quorum loss'

### etcd-recovery.md (score: 0.91)
If quorum is lost with 2 of 3 control plane nodes down,
restore from the most recent etcd snapshot...

$ sre incident start "qdrant pod crashlooping"
[12:34] Incident opened: qdrant pod crashlooping
[12:34] Cluster snapshot saved to vault
```

## MCP Server

The same tools are exposed as an [MCP server](https://modelcontextprotocol.io/), allowing Claude Code to query the cluster and search the knowledge base mid-conversation.

```bash
# Register with Claude Code
claude mcp add homelab-sre -- sre-mcp

# Or run directly
sre-mcp
```

**Tools exposed:**

| Tool | Description |
|---|---|
| `get_cluster_status` | Node status, firing alerts, high-restart pods |
| `get_alerts` | Active Alertmanager alerts with severity |
| `prometheus_query` | Arbitrary PromQL execution |
| `search_vault` | Semantic search over the Qdrant knowledge base (falls back to grep) |

With the MCP server registered, you can ask Claude Code: *"What alerts are firing?"* or *"Find the runbook for certificate renewal"* — it calls the tools in the background and responds with live context.

## Installation

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/t12-pybash/sre-cli
cd sre-cli
uv pip install -e .
```

## Configuration

Config is loaded from `~/.config/sre/config.toml`. All values can also be set via environment variables prefixed with `SRE_`.

```toml
# ~/.config/sre/config.toml

prometheus_url    = "https://prometheus.your-cluster.example"
alertmanager_url  = "https://alertmanager.your-cluster.example"
qdrant_url        = "https://qdrant.your-cluster.example"
embed_ollama_url  = "http://localhost:11434"
embed_model       = "nomic-embed-text"
vault_path        = "/home/user/Documents/obsidian-vault"
ollama_url        = "https://litellm.your-cluster.example"
ollama_model      = "llama3.2"
ollama_api_key    = "your-litellm-key"
```

### Knowledge base ingestion

Ingest a directory of Markdown files into Qdrant:

```bash
python sre/scripts/ingest_vault.py
```

Chunks files by paragraph, embeds with `nomic-embed-text` via Ollama, and upserts into a Qdrant collection named `vault` (768-dimensional, cosine distance).

## Stack

- [Typer](https://typer.tiangolo.com/) — CLI framework
- [Rich](https://rich.readthedocs.io/) — terminal output formatting
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server
- [httpx](https://www.python-httpx.org/) — async HTTP client
- [Qdrant](https://qdrant.tech/) — vector database
- [Ollama](https://ollama.ai/) — local LLM and embedding inference

## Related

- [t-12.io](https://t-12.io) — infrastructure consultancy this tool was built for
- [Private AI Platform post](https://t-12.io/solutions/private-ai-platform/) — full write-up of the stack
