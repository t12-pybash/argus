import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from sre.clients.alertmanager import AlertmanagerClient
from sre.clients.prometheus import PrometheusClient
from sre import config as cfg_module

console = Console()

ACTIVE_FILE = Path.home() / ".config" / "sre" / "active_incident.json"


def _incident_dir(vault_path: Path) -> Path:
    d = vault_path / "3 - Resources" / "Homelab" / "Incidents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(title: str) -> str:
    return title.lower().replace(" ", "-").replace("/", "-")[:60]


def _cluster_snapshot(cfg: dict) -> str:
    lines = []
    try:
        prom = PrometheusClient(cfg["prometheus_url"])
        nodes = prom.nodes_up()
        lines.append("### Node Status")
        for n in nodes:
            state = "UP" if n["value"][1] == "1" else "DOWN"
            lines.append(f"- {n['metric'].get('instance', '?')}: {state}")
    except Exception:
        lines.append("### Node Status\n- (unavailable)")

    try:
        am = AlertmanagerClient(cfg["alertmanager_url"])
        alerts = [a for a in am.alerts() if a.get("labels", {}).get("alertname") != "Watchdog"]
        lines.append("\n### Firing Alerts")
        if alerts:
            for a in alerts:
                name = a.get("labels", {}).get("alertname", "?")
                sev = a.get("labels", {}).get("severity", "?")
                summary = a.get("annotations", {}).get("summary", "")
                lines.append(f"- **{name}** [{sev}] {summary}")
        else:
            lines.append("- None (only Watchdog)")
    except Exception:
        lines.append("### Firing Alerts\n- (unavailable)")

    return "\n".join(lines)


def start(title: str):
    cfg = cfg_module.load()
    vault_path = Path(cfg["vault_path"])
    ts = datetime.now().strftime("%Y-%m-%d-%H%M")
    slug = _slug(title)
    filename = f"{ts}-{slug}.md"
    path = _incident_dir(vault_path) / filename

    snapshot = _cluster_snapshot(cfg)
    started_at = _now()

    content = f"""# Incident: {title}

**Started:** {started_at}
**Status:** 🔴 Active

## Cluster State at Start

{snapshot}

## Timeline

- `{started_at}` — Incident started: {title}

## Resolution

_Not yet resolved._
"""
    path.write_text(content)

    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(json.dumps({"path": str(path), "title": title, "started": started_at}))

    console.print(f"[bold red]🚨 Incident started:[/bold red] {title}")
    console.print(f"[dim]Note file:[/dim] {path}")


def note(message: str):
    if not ACTIVE_FILE.exists():
        console.print("[yellow]No active incident. Run `sre incident start <title>` first.[/yellow]")
        return

    state = json.loads(ACTIVE_FILE.read_text())
    path = Path(state["path"])
    ts = _now()

    text = path.read_text()
    entry = f"- `{ts}` — {message}\n"
    text = text.replace("## Resolution", f"{entry}\n## Resolution")
    path.write_text(text)

    console.print(f"[green]✓[/green] Note added to [bold]{state['title']}[/bold]")


def close(summary: str):
    if not ACTIVE_FILE.exists():
        console.print("[yellow]No active incident.[/yellow]")
        return

    state = json.loads(ACTIVE_FILE.read_text())
    path = Path(state["path"])
    resolved_at = _now()

    text = path.read_text()
    text = text.replace("**Status:** 🔴 Active", "**Status:** ✅ Resolved")
    text = text.replace("_Not yet resolved._", f"**Resolved:** {resolved_at}\n\n{summary}")
    entry = f"- `{resolved_at}` — Resolved: {summary}\n"
    text = text.replace("## Resolution", f"{entry}\n## Resolution")
    path.write_text(text)

    ACTIVE_FILE.unlink()
    console.print(f"[bold green]✅ Incident closed:[/bold green] {state['title']}")
    console.print(f"[dim]{path}[/dim]")


def show():
    if not ACTIVE_FILE.exists():
        console.print("[dim]No active incident.[/dim]")
        return
    state = json.loads(ACTIVE_FILE.read_text())
    t = Table(show_header=False, box=None)
    t.add_row("[bold]Title[/bold]", state["title"])
    t.add_row("[bold]Started[/bold]", state["started"])
    t.add_row("[bold]File[/bold]", state["path"])
    console.print(t)
