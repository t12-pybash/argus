from concurrent.futures import ThreadPoolExecutor
from rich.console import Console
from rich.table import Table
from rich import box
from argus.clients.prometheus import PrometheusClient
from argus import config as cfg_module

console = Console()


def status():
    cfg = cfg_module.load()
    prom = PrometheusClient(cfg["prometheus_url"])

    console.rule("[bold blue]Homelab Status")

    with ThreadPoolExecutor(max_workers=3) as executor:
        f_nodes = executor.submit(prom.nodes_up)
        f_alerts = executor.submit(prom.firing_alerts)
        f_restarts = executor.submit(prom.pod_restarts)

    # Nodes
    table = Table(title="Nodes", box=box.SIMPLE_HEAVY)
    table.add_column("Instance", style="cyan")
    table.add_column("Job")
    table.add_column("Status", justify="center")

    try:
        for n in f_nodes.result():
            up = n["value"][1] == "1"
            status_str = "[green]UP[/green]" if up else "[red]DOWN[/red]"
            table.add_row(n["metric"].get("instance", "?"), n["metric"].get("job", "?"), status_str)
        console.print(table)
    except Exception as e:
        console.print(f"[red]Prometheus unreachable:[/red] {e}")

    # Alerts
    try:
        alerts = f_alerts.result()
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
        restarts = f_restarts.result()
        if restarts:
            console.print(f"[yellow]⚠ {len(restarts)} pod(s) with >5 restarts[/yellow]")
            for r in restarts:
                m = r["metric"]
                console.print(f"  • {m.get('namespace','?')}/{m.get('pod','?')}")
    except Exception:
        pass
