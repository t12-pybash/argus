# argus

Autonomous SRE agent for Kubernetes — sovereign, on-premises, no data leaves the cluster.

Built by a Lead SRE with 14 years of production infrastructure experience. Argus combines live cluster observability, semantic search over a local knowledge base, LLM-powered diagnosis, and an autonomous remediation agent with human-in-the-loop control.

Every competitor (Datadog Bits AI, Dynatrace Davis, PagerDuty AI) sends your operational data to an external cloud. Argus doesn't.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                    Argus                        │
│                                                 │
│  argus status    ──▶  Prometheus                │
│  argus alert     ──▶  Alertmanager              │
│  argus ask       ──▶  LiteLLM / Ollama          │
│  argus diagnose  ──▶  kubectl + LLM             │
│  argus runbook   ──▶  Qdrant (vector search)    │
│  argus incident  ──▶  Qdrant + vault            │
│  argus agent     ──▶  Alertmanager + kubectl    │
│                       + LLM + Slack             │
└─────────────────────────────────────────────────┘
         │
         ▼
  Qdrant knowledge base
  (runbooks, incident history, architecture docs)
  ingested in real-time via Redpanda
```

## Commands

```
argus status                            Node health, firing alerts, high-restart pods
argus alert                             Active Alertmanager alerts in plain English
argus ask "<question>"                  LLM query with live Prometheus context
argus diagnose <namespace/pod>          Logs + events + LLM root cause analysis
argus diagnose <namespace/pod> --save   Diagnose and index result to Qdrant
argus runbook <query>                   Semantic search over runbooks and vault notes
argus runbook <query> --ask             Same, with LLM synthesis of top results
argus incident start "<title>"          Open incident, snapshot cluster state
argus incident note "<message>"         Append timestamped observation
argus incident close "<summary>"        Close with resolution summary
argus incident show                     Show active incident
argus agent start                       Start autonomous daemon (human-in-the-loop)
argus agent start --auto                Start in full-auto remediation mode
argus agent approve                     Approve pending remediation action
argus agent reject                      Reject pending remediation action
argus agent skip                        Skip current alert
argus agent status                      Show agent state and pending action
```

## Agent

The agent daemon watches Alertmanager, diagnoses alerts automatically, and proposes remediation:

```
Alert fires → agent wakes → argus diagnose → search runbooks
           → LLM picks action → notifies Slack → awaits approval
           → executes → indexes outcome to Qdrant
```

Supported remediation actions:

| Action | Trigger |
|---|---|
| `rollout_restart` | Pod crashlooping or stuck |
| `rollout_undo` | New rollout caused the alert |
| `force_delete_pod` | Pod stuck in Terminating or Unknown |
| `scale_deployment` | Resource pressure |
| `expand_pvc` | PVC near capacity |
| `delete_failed_jobs` | Failed batch jobs accumulating |
| `cordon_node` | Node unhealthy |
| `drain_node` | Node needs evacuating |
| `flux_reconcile` | Flux sync stuck or drifted |
| `flux_suspend` | HelmRelease looping upgrade failures |
| `restart_coredns` | DNS resolution failures cluster-wide |

Human approval is required by default. Use `--auto` to execute immediately.

## Installation

```bash
pip install argus-sre
```

Or from source (requires Python 3.11+ and [uv](https://github.com/astral-sh/uv)):

```bash
git clone https://github.com/t12-pybash/argus
cd argus
uv pip install -e .
```

## Configuration

Config is loaded from `~/.config/argus/config.toml`. All values can be overridden with `ARGUS_` environment variables.

```toml
prometheus_url        = "https://prometheus.your-cluster.example"
alertmanager_url      = "https://alertmanager.your-cluster.example"
qdrant_url            = "https://qdrant.your-cluster.example"
ollama_url            = "https://litellm.your-cluster.example"
ollama_model          = "llama3.2"
embed_ollama_url      = "http://localhost:11434"
embed_model           = "nomic-embed-text"
vault_path            = "/home/user/Documents/obsidian-vault"
agent_webhook_url     = "https://hooks.slack.com/services/..."
agent_approval_timeout = "300"
```

## Knowledge Base

Argus maintains a Qdrant vector knowledge base of runbooks, incident history, and architecture docs. The `argus runbook` and `argus diagnose --save` commands search and write to this base, so the agent gets smarter with every incident.

Real-time ingestion via Redpanda keeps the index in sync as vault documents are updated.

## MCP Server

Argus exposes an MCP server for use with Claude Code and other AI assistants:

```bash
claude mcp add argus -- argus-mcp
```

| Tool | Description |
|---|---|
| `get_cluster_status` | Node status, firing alerts, high-restart pods |
| `get_alerts` | Active Alertmanager alerts with severity |
| `prometheus_query` | Arbitrary PromQL execution |
| `search_vault` | Semantic search over the Qdrant knowledge base |

## Stack

- [Typer](https://typer.tiangolo.com/) — CLI framework
- [Rich](https://rich.readthedocs.io/) — terminal output
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server
- [httpx](https://www.python-httpx.org/) — HTTP client
- [Qdrant](https://qdrant.tech/) — vector database
- [Ollama](https://ollama.ai/) / [LiteLLM](https://github.com/BerriAI/litellm) — local LLM inference

---

*Ciarán Donegan — Lead SRE | [LinkedIn](https://www.linkedin.com/in/cdonegan7/) | [t-12.io](https://t-12.io)*
