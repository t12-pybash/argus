from pathlib import Path
from rich.console import Console
from argus import config as cfg_module

try:
    from argus.clients.qdrant import upsert_file
    _qdrant = True
except ImportError:
    _qdrant = False

console = Console()


def sync():
    if not _qdrant:
        console.print("[red]Install argus-sre[rag] to use vault sync.[/red]")
        return

    cfg = cfg_module.load()
    vault_path = Path(cfg.get("vault_path", ""))
    qdrant_url = cfg.get("qdrant_url", "")
    ollama_url = cfg.get("ollama_url", "")
    embed_model = cfg.get("embed_model", "nomic-embed-text")

    if not vault_path or not vault_path.exists():
        console.print(f"[red]vault_path not set or not found: {vault_path or '(not set)'}[/red]")
        console.print("[dim]Set vault_path in ~/.config/argus/config.toml[/dim]")
        return

    md_files = list(vault_path.rglob("*.md"))
    if not md_files:
        console.print(f"[yellow]No markdown files found in {vault_path}[/yellow]")
        return

    console.print(f"Found [cyan]{len(md_files)}[/cyan] files in [dim]{vault_path}[/dim]")

    indexed, skipped, errors = 0, 0, 0

    with console.status("[cyan]Syncing...") as s:
        for i, path in enumerate(md_files):
            rel_path = str(path.relative_to(vault_path))
            s.update(f"[cyan]({i + 1}/{len(md_files)}) {rel_path[:70]}")
            try:
                content = path.read_text(encoding="utf-8", errors="ignore").strip()
                if not content:
                    skipped += 1
                    continue
                upsert_file(qdrant_url, ollama_url, embed_model, rel_path, content)
                indexed += 1
            except Exception as e:
                errors += 1
                console.log(f"[red]Error: {rel_path}: {e}[/red]")

    parts = [f"[green]✓ {indexed} indexed[/green]"]
    if skipped:
        parts.append(f"[dim]{skipped} empty files skipped[/dim]")
    if errors:
        parts.append(f"[red]{errors} errors[/red]")
    console.print(" | ".join(parts))
