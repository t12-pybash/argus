from concurrent.futures import ThreadPoolExecutor
from rich.console import Console
from rich.table import Table
from rich import box
from argus.clients.prometheus import PrometheusClient
from argus.clients import kubernetes as k8s
from argus import config as cfg_module

console = Console()


def status():
    cfg = cfg_module.load()
    prom = PrometheusClient(cfg["prometheus_url"])

    console.rule("[bold blue]Homelab Status")

    with ThreadPoolExecutor(max_workers=4) as executor:
        f_nodes = executor.submit(prom.nodes_up)
        f_alerts = executor.submit(prom.firing_alerts)
        f_restarts = executor.submit(prom.pod_restarts)
        f_isvcs = executor.submit(k8s.inference_services)

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

    # InferenceServices (KServe)
    try:
        isvcs = f_isvcs.result()
        if isvcs:
            table = Table(title="Inference Services (KServe)", box=box.SIMPLE_HEAVY)
            table.add_column("Namespace", style="cyan")
            table.add_column("Name", style="cyan")
            table.add_column("Ready", justify="center")
            table.add_column("URL", style="dim")
            for isvc in isvcs:
                ready_str = "[green]✓ Ready[/green]" if isvc["ready"] else "[red]✗ Not Ready[/red]"
                table.add_row(isvc["namespace"], isvc["name"], ready_str, isvc["url"])
            console.print(table)
    except Exception as e:
        console.print(f"[red]ISVC error: {e}[/red]")
