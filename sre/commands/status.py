import typer
from rich.console import Console
from rich.table import Table
from rich import box
from sre.clients.prometheus import PrometheusClient
from sre import config as cfg_module

console = Console()


def status():
    cfg = cfg_module.load()
    prom = PrometheusClient(cfg["prometheus_url"])

    console.rule("[bold blue]Homelab Status")

    # Nodes
    table = Table(title="Nodes", box=box.SIMPLE_HEAVY)
    table.add_column("Instance", style="cyan")
    table.add_column("Job")
    table.add_column("Status", justify="center")

    try:
        nodes = prom.nodes_up()
        for n in nodes:
            up = n["value"][1] == "1"
            status_str = "[green]UP[/green]" if up else "[red]DOWN[/red]"
            table.add_row(n["metric"].get("instance", "?"), n["metric"].get("job", "?"), status_str)
        console.print(table)
    except Exception as e:
        console.print(f"[red]Prometheus unreachable:[/red] {e}")

    # Alerts
    try:
        alerts = prom.firing_alerts()
        if alerts:
            console.print(f"[red bold]⚠ {len(alerts)} firing alert(s)[/red bold]")
            for a in alerts:
                console.print(f"  • {a['metric'].get('alertname', '?')}")
        else:
            console.print("[green]✓ No firing alerts[/green]")
    except Exception as e:
        console.print(f"[yellow]Could not fetch alerts:[/yellow] {e}")

    # High restart pods
    try:
        restarts = prom.pod_restarts()
        if restarts:
            console.print(f"[yellow]⚠ {len(restarts)} pod(s) with >5 restarts[/yellow]")
            for r in restarts:
                m = r["metric"]
                console.print(f"  • {m.get('namespace','?')}/{m.get('pod','?')}")
    except Exception:
        pass
