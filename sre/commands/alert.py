from rich.console import Console
from rich.table import Table
from rich import box
from sre.clients.alertmanager import AlertmanagerClient
from sre import config as cfg_module

console = Console()


def alert():
    cfg = cfg_module.load()
    client = AlertmanagerClient(cfg["alertmanager_url"])

    try:
        alerts = client.alerts()
    except Exception as e:
        console.print(f"[red]Alertmanager unreachable:[/red] {e}")
        raise SystemExit(1)

    firing = [a for a in alerts if a.get("status", {}).get("state") == "active"]

    if not firing:
        console.print("[green]✓ No active alerts[/green]")
        return

    table = Table(title=f"{len(firing)} Active Alert(s)", box=box.SIMPLE_HEAVY)
    table.add_column("Alert", style="red bold")
    table.add_column("Severity")
    table.add_column("Namespace")
    table.add_column("Since")

    for a in firing:
        labels = a.get("labels", {})
        table.add_row(
            labels.get("alertname", "?"),
            labels.get("severity", "-"),
            labels.get("namespace", "-"),
            a.get("startsAt", "-")[:19].replace("T", " "),
        )

    console.print(table)
