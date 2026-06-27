from rich.console import Console
from rich.table import Table
from rich import box
from argus.clients import kubernetes as k8s

console = Console()


def status():
    console.rule("[bold blue]Inference Layer")

    isvcs = k8s.inference_services()
    if not isvcs:
        console.print("[yellow]No InferenceServices found — is KServe installed?[/yellow]")
        return

    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("Namespace", style="cyan")
    table.add_column("Name", style="cyan")
    table.add_column("Ready", justify="center")
    table.add_column("Image", style="dim")

    for isvc in isvcs:
        image = k8s.inference_service_image(isvc["namespace"], isvc["name"])
        if "@sha256:" in image:
            repo, digest = image.split("@sha256:")
            image_display = f"{repo}@sha256:{digest[:12]}…"
        else:
            image_display = image
        ready_str = "[green]✓ Ready[/green]" if isvc["ready"] else "[red]✗ Not Ready[/red]"
        table.add_row(isvc["namespace"], isvc["name"], ready_str, image_display)

    console.print(table)

    for isvc in isvcs:
        models = k8s.ollama_models(isvc["namespace"], isvc["name"])
        if models:
            console.print(f"[bold]Models in {isvc['name']}:[/bold]")
            for m in models:
                console.print(f"  • {m['name']}  [dim]({m.get('size', '')})[/dim]")
        else:
            console.print(f"[yellow]Could not list models for {isvc['name']}[/yellow]")


def rollout(name: str, image: str, namespace: str):
    console.print(f"Updating [cyan]{namespace}/{name}[/cyan] → [bold]{image}[/bold]")
    result = k8s.patch_inference_service_image(namespace, name, image)
    if "updated" in result.lower():
        console.print(f"[green]✓[/green] {result}")
        console.print(
            f"[dim]KServe is rolling out the new image. Watch with:[/dim]\n"
            f"[dim]kubectl get inferenceservice {name} -n {namespace} -w[/dim]"
        )
    else:
        console.print(f"[red]✗[/red] {result}")


def rollback(name: str, namespace: str):
    console.print(f"Rolling back [cyan]{namespace}/{name}[/cyan]…")
    result = k8s.rollback_inference_service(namespace, name)
    if "rolled back" in result.lower():
        console.print(f"[green]✓[/green] {result}")
    else:
        console.print(f"[red]✗[/red] {result}")
