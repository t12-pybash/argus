# sre-cli

A command-line SRE tool and MCP server built for operating a self-hosted Kubernetes cluster — combining live cluster observability with semantic search over a local knowledge base, an LLM query interface, and an incident lifecycle manager.

Built by a Lead SRE with 14 years of production infrastructure experience as a practical replacement for the context-switching overhead of dashboards, Slack, and runbook wikis during incidents. All queries stay on-cluster — no data leaves the environment.

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

The knowledge base is an [Obsidian](https://obsidian.md/) vault ingested into [Qdrant](https://qdrant.tech/) via event-driven pipeline (Redpanda → consumer → embeddings via `nomic-embed-text`). `sre runbook` performs semantic vector search over this vault — finding relevant runbooks, incident notes, and architecture docs by meaning rather than keyword.

## Commands

```
sre status                        Node health, firing alerts, high-restart pods
sre alert                         Active Alertmanager alerts
sre runbook <query>               Semantic search over runbooks and vault notes
sre runbook <query> --ask         Same, with LLM synthesis of top results
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
Vault search: etcd quorum loss

╭─── etcd-recovery.md  score: 0.91 ─────────────────────────╮
│ If quorum is lost with 2 of 3 control plane nodes down,    │
│ restore from the most recent etcd snapshot...              │
╰─────────────────────────────────────────────────────────────╯

$ sre incident start "qdrant pod crashlooping"
[12:34] Incident opened: qdrant pod crashlooping
[12:34] Cluster snapshot saved to vault
```

## MCP Server

The same tools are exposed as an [MCP server](https://modelcontextprotocol.io/), allowing Claude Code to query the cluster and search the knowledge base mid-conversation.

```bash
# Register with Claude Code
claude mcp add homelab-sre -- sre-mcp
```

**Tools exposed:**

| Tool | Description |
|---|---|
| `get_cluster_status` | Node status, firing alerts, high-restart pods |
| `get_alerts` | Active Alertmanager alerts with severity |
| `prometheus_query` | Arbitrary PromQL execution |
| `search_vault` | Semantic search over the Qdrant knowledge base |

With the MCP server registered, Claude Code can answer: *"What alerts are firing?"* or *"Find the runbook for certificate renewal"* — calling the tools in the background with live cluster context.

## Installation

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/t12-pybash/sre-cli
cd sre-cli
uv pip install -e .
```

## Configuration

Config is loaded from `~/.config/sre/config.toml`. All values can be overridden with `SRE_` environment variables.

```toml
prometheus_url    = "https://prometheus.your-cluster.example"
alertmanager_url  = "https://alertmanager.your-cluster.example"
qdrant_url        = "https://qdrant.your-cluster.example"
embed_ollama_url  = "http://localhost:11434"
embed_model       = "nomic-embed-text"
vault_path        = "/home/user/Documents/obsidian-vault"
ollama_url        = "https://litellm.your-cluster.example"
ollama_model      = "llama3.2"
```

## Knowledge Base Ingestion

Ingest Markdown files into Qdrant:

```bash
python sre/scripts/ingest_vault.py
```

Chunks files by paragraph, embeds with `nomic-embed-text` via Ollama, upserts into a Qdrant collection named `vault`. Point IDs are deterministic (MD5 of `rel_path:chunk_index`) so re-ingestion is idempotent. An event-driven consumer (`consume_vault.py`) keeps the index in sync in real-time via Redpanda.

## Stack

- [Typer](https://typer.tiangolo.com/) — CLI framework
- [Rich](https://rich.readthedocs.io/) — terminal output formatting
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server
- [httpx](https://www.python-httpx.org/) — HTTP client
- [Qdrant](https://qdrant.tech/) — vector database
- [Ollama](https://ollama.ai/) — local LLM and embedding inference

## Related Repos

- [homelab-gitops](https://github.com/t12-pybash/homelab-gitops) — the cluster this tool operates (Flux GitOps, Cilium, private AI platform)
- [fullnode-infrastructure](https://github.com/t12-pybash/fullnode-infrastructure) — production blockchain node infrastructure (OP Stack, Kubernetes, Terraform)

---

*Ciarán Donegan — Lead SRE | [LinkedIn](https://www.linkedin.com/in/cdonegan7/) | [t-12.io](https://t-12.io)*
