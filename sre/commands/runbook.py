import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich import box
from sre.clients.ollama import OllamaClient
from sre import config as cfg_module

console = Console()


def _embed(text: str, ollama_url: str, model: str) -> list[float]:
    resp = httpx.post(
        f"{ollama_url}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _search(vec: list[float], qdrant_url: str, limit: int = 5) -> list[dict]:
    resp = httpx.post(
        f"{qdrant_url}/collections/vault/points/search",
        json={"vector": vec, "limit": limit, "with_payload": True},
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def runbook(query: str, ask: bool = False):
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

    if ask:
        context = "\n\n---\n\n".join(chunks_for_llm)
        prompt = f"""You are an SRE assistant. The user searched their runbook vault for: "{query}"

Here are the most relevant vault notes:

{context}

Summarise the relevant steps or information concisely. Focus on actionable content."""
        llm = OllamaClient(cfg["ollama_url"], cfg["ollama_model"], cfg.get("ollama_api_key", ""))
        with console.status("[cyan]Synthesising answer..."):
            response = llm.ask(prompt)
        console.print("\n")
        console.print(Markdown(response))
