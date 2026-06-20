import subprocess
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from argus.clients.alertmanager import AlertmanagerClient
from argus.clients.prometheus import PrometheusClient
from argus import config as cfg_module

mcp = FastMCP("homelab-sre")
_cfg = cfg_module.load()
_prom = PrometheusClient(_cfg["prometheus_url"])
_am = AlertmanagerClient(_cfg["alertmanager_url"])
_vault = Path(_cfg["vault_path"])


@mcp.tool()
def get_cluster_status() -> str:
    """Get current cluster status: node health, firing alerts, pods with high restarts."""
    nodes = _prom.nodes_up()
    alerts = _prom.firing_alerts()
    restarts = _prom.pod_restarts()

    lines = ["## Cluster Status"]
    lines.append("\n### Nodes")
    for n in nodes:
        name = n["metric"].get("instance", "?")
        state = "UP" if n["value"][1] == "1" else "DOWN"
        lines.append(f"- {name}: {state}")

    lines.append(f"\n### Firing Alerts: {len(alerts)}")
    for a in alerts:
        lines.append(f"- {a['metric'].get('alertname', '?')}")

    lines.append("\n### Pods with >5 restarts")
    for p in restarts:
        ns = p["metric"].get("namespace", "?")
        pod = p["metric"].get("pod", "?")
        count = p["value"][1]
        lines.append(f"- {ns}/{pod}: {count} restarts")

    return "\n".join(lines)


@mcp.tool()
def get_alerts() -> str:
    """Get active Alertmanager alerts with severity and details."""
    alerts = _am.alerts()
    if not alerts:
        return "No active alerts."

    lines = ["## Active Alerts"]
    for a in alerts:
        name = a.get("labels", {}).get("alertname", "?")
        severity = a.get("labels", {}).get("severity", "?")
        ns = a.get("labels", {}).get("namespace", "-")
        summary = a.get("annotations", {}).get("summary", "")
        lines.append(f"- **{name}** [{severity}] ns={ns} — {summary}")
    return "\n".join(lines)


@mcp.tool()
def prometheus_query(promql: str) -> str:
    """Run an arbitrary PromQL query against the cluster Prometheus."""
    results = _prom.query(promql)
    if not results:
        return "No results."
    lines = [f"## Query: `{promql}`"]
    for r in results:
        labels = ", ".join(f"{k}={v}" for k, v in r["metric"].items())
        value = r["value"][1]
        lines.append(f"- {labels}: {value}")
    return "\n".join(lines)


@mcp.tool()
def search_vault(query: str) -> str:
    """Search Obsidian vault using semantic vector search (falls back to grep if Qdrant unavailable)."""
    if not _vault.exists():
        return f"Vault not found at {_vault}"

    try:
        embed_resp = httpx.post(
            f"{_cfg['embed_ollama_url']}/api/embeddings",
            json={"model": _cfg["embed_model"], "prompt": query},
            timeout=15,
        )
        embed_resp.raise_for_status()
        vec = embed_resp.json()["embedding"]

        search_resp = httpx.post(
            f"{_cfg['qdrant_url']}/collections/vault/points/search",
            json={"vector": vec, "limit": 5, "with_payload": True},
            verify=False,
            timeout=15,
        )
        search_resp.raise_for_status()
        hits = search_resp.json().get("result", [])

        if not hits:
            return f"No semantic results found for '{query}'."

        lines = [f"## Vault search (semantic): '{query}'\n"]
        for hit in hits:
            lines.append(f"### {hit['payload']['file']} (score: {hit['score']:.2f})")
            lines.append(hit["payload"]["text"][:400])
            lines.append("")
        return "\n".join(lines)

    except Exception:
        result = subprocess.run(
            ["grep", "-r", "-l", "-i", query, str(_vault), "--include=*.md"],
            capture_output=True, text=True, timeout=10
        )
        files = [f for f in result.stdout.strip().splitlines() if f]
        if not files:
            return f"No vault files found matching '{query}'."
        lines = [f"## Vault search (grep): '{query}'", f"Found {len(files)} file(s):\n"]
        for f in files[:5]:
            path = Path(f)
            lines.append(f"### {path.name}")
            grep = subprocess.run(["grep", "-i", "-n", query, f], capture_output=True, text=True, timeout=5)
            for line in grep.stdout.splitlines()[:5]:
                lines.append(f"  {line}")
            lines.append("")
        return "\n".join(lines)
