import httpx
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich import box
from argus.clients.ollama import OllamaClient
from argus.clients.qdrant import upsert_file
from argus import config as cfg_module

console = Console()


def _embed(text: str, ollama_url: str, model: str) -> list[float]:
    resp = httpx.post(
        f"{ollama_url}/api/embed",
        json={"model": model, "input": text},
        verify=False,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


def _search(vec: list[float], qdrant_url: str, limit: int = 5) -> list[dict]:
    resp = httpx.post(
        f"{qdrant_url}/collections/vault/points/search",
        json={"vector": vec, "limit": limit, "with_payload": True},
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def _save_runbook(vault_path: Path, topic: str, content: str) -> Path:
    runbook_dir = vault_path / "3 - Resources" / "Homelab" / "Runbooks"
    runbook_dir.mkdir(parents=True, exist_ok=True)
    slug = topic.lower().replace(" ", "-").replace("/", "-")[:60]
    ts = datetime.now().strftime("%Y-%m-%d")
    path = runbook_dir / f"{ts}-{slug}.md"
    path.write_text(f"# Runbook: {topic}\n\n*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n{content}\n")
    return path


def runbook(query: str, ask: bool = False, generate: bool = False):
    cfg = cfg_module.load()
    ollama_url = cfg.get("embed_ollama_url", "http://localhost:11434")
    embed_model = cfg.get("embed_model", "nomic-embed-text")
    qdrant_url = cfg["qdrant_url"]

    with console.status("[cyan]Searching vault..."):
        vec = _embed(query, ollama_url, embed_model)
        hits = _search(vec, qdrant_url)

    if not hits:
        console.print("[yellow]No results found.[/yellow]")
        return

    console.print(f"\n[bold cyan]Vault search:[/bold cyan] {query}\n")
    chunks_for_llm = []
    for hit in hits:
        file = hit["payload"]["file"]
        score = hit["score"]
        text = hit["payload"]["text"]
        chunks_for_llm.append(f"[{file}]\n{text}")
        console.print(Panel(
            text[:500],
            title=f"[green]{file}[/green]  [dim]score: {score:.2f}[/dim]",
            box=box.ROUNDED,
            expand=False,
        ))

    llm = OllamaClient(cfg["ollama_url"], cfg["ollama_model"], cfg.get("ollama_api_key", ""))
    ask_chunks = [f"[{h['payload']['file']}]\n{h['payload']['text']}"
                  for h in hits if h["score"] >= 0.62]
    context = "\n\n---\n\n".join(ask_chunks or chunks_for_llm)

    if ask:
        prompt = f"""You are an SRE assistant. The user searched their runbook vault for: "{query}"

Here are the most relevant vault notes:

{context}

Give a concise answer with the root cause and exact fix steps. Be direct and practical."""
        with console.status("[cyan]Synthesising answer..."):
            response = llm.ask(prompt)
        console.print("\n")
        console.print(Markdown(response))

    if generate:
        prompt = f"""You are a senior SRE writing a structured runbook for: "{query}"

Use these existing vault notes as context:

{context}

Write a complete runbook in markdown with these sections:
## Symptoms
## Root Cause
## Diagnosis Steps
## Fix Steps
## Verification
## Prevention

Be specific and include exact commands. This will be saved and used by the team."""
        with console.status("[cyan]Generating runbook..."):
            response = llm.ask(prompt)
        console.print("\n")
        console.print(Markdown(response))
        vault_path = Path(cfg["vault_path"])
        path = _save_runbook(vault_path, query, response)
        console.print(f"\n[green]✓ Saved:[/green] {path}")
        try:
            rel_path = str(path.relative_to(vault_path))
            with console.status("[cyan]Indexing into Qdrant..."):
                upsert_file(
                    cfg["qdrant_url"],
                    cfg.get("embed_ollama_url", cfg["ollama_url"]),
                    cfg.get("embed_model", "nomic-embed-text"),
                    rel_path,
                    response,
                )
            console.print("[green]✓ Indexed — searchable now via `argus runbook`.[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠ Qdrant index failed ({e}). Will be picked up on next vault sync.[/yellow]")
