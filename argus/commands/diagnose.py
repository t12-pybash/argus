from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich import box

from argus.clients.kubernetes import pod_describe, pod_logs_previous, pod_events
from argus.clients.ollama import OllamaClient
from argus.clients.qdrant import upsert_file
from argus import config as cfg_module

console = Console()


def _truncate(text: str, max_lines: int) -> str:
    lines = text.strip().splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[-max_lines:])
    return text.strip()


def _save_runbook(vault_path: Path, namespace: str, pod: str, analysis: str):
    runbook_dir = vault_path / "3 - Resources" / "Homelab" / "Runbooks"
    runbook_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{namespace}-{pod}".replace("/", "-")[:60]
    ts = datetime.now().strftime("%Y-%m-%d")
    filename = f"{ts}-diagnose-{slug}.md"
    path = runbook_dir / filename
    content = f"""# Diagnosis: {namespace}/{pod}

*Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}*

{analysis}
"""
    path.write_text(content)
    return path


def diagnose(pod_ref: str, save: bool = False):
    if "/" in pod_ref:
        namespace, pod = pod_ref.split("/", 1)
    else:
        namespace, pod = "default", pod_ref

    console.print(f"\n[bold cyan]Diagnosing[/bold cyan] {namespace}/{pod}\n")

    with console.status("[cyan]Gathering pod data..."):
        with ThreadPoolExecutor(max_workers=3) as executor:
            f_describe = executor.submit(pod_describe, namespace, pod)
            f_logs = executor.submit(pod_logs_previous, namespace, pod, 50)
            f_events = executor.submit(pod_events, namespace, pod)
        describe = f_describe.result()
        logs = f_logs.result()
        events = f_events.result()

    if not describe:
        console.print(f"[red]Pod {namespace}/{pod} not found.[/red]")
        return

    console.print(Panel(
        _truncate(events, 20) or "(no events)",
        title="[yellow]Recent Events[/yellow]",
        box=box.ROUNDED,
        expand=False,
    ))
    console.print(Panel(
        _truncate(logs, 30) or "(no previous logs)",
        title="[yellow]Previous Logs (last 30 lines)[/yellow]",
        box=box.ROUNDED,
        expand=False,
    ))

    describe_trimmed = _truncate(describe, 80)
    prompt = f"""You are a Kubernetes SRE diagnosing a pod that is crashing or restarting.

Pod: {namespace}/{pod}

--- kubectl describe (trimmed) ---
{describe_trimmed}

--- Previous logs (last 50 lines) ---
{logs or "(none)"}

--- Recent events ---
{events or "(none)"}

Diagnose this specific pod using only the evidence above. Do not give generic Kubernetes advice.

Relevant Kubernetes facts to apply when interpreting the evidence:
- DNSConfigForming / "Nameserver limits were exceeded": The node has TOO MANY nameservers in /etc/resolv.conf. Kubernetes caps the total at 3. The fix is to REMOVE excess nameservers from /etc/resolv.conf on the affected node so only 3 remain. Do NOT add nameservers — that makes it worse. Steps: (1) find the node with `kubectl get pod <pod> -n <ns> -o jsonpath='{{.spec.nodeName}}'`, (2) SSH to it, (3) edit /etc/resolv.conf to leave only 3 nameserver lines.
- OOMKilled: container hit its memory limit. Fix: raise resources.limits.memory or reduce memory usage.
- CrashLoopBackOff with exit code 1: application error — check logs for the actual error message.
- ImagePullBackOff: image not found or registry unreachable. Fix: verify image name/tag and registry credentials.

1. Root cause: quote the specific log line or event that tells you what is wrong.
2. Fix steps: exact shell commands to resolve the specific issue shown. Name the exact file, node, or service.
3. Prevention: one or two specific measures relevant to this failure mode only.

If the evidence is insufficient to determine the root cause, say so — do not speculate."""

    cfg = cfg_module.load()
    ollama = OllamaClient(cfg["ollama_url"], cfg["ollama_model"], cfg.get("ollama_api_key", ""))

    with console.status("[cyan]Analysing..."):
        analysis = ollama.ask(prompt)

    console.print("\n")
    console.print(Markdown(analysis))

    if save:
        vault_path = Path(cfg["vault_path"])
        path = _save_runbook(vault_path, namespace, pod, analysis)
        console.print(f"\n[green]✓ Saved:[/green] {path}")
        try:
            rel_path = str(path.relative_to(vault_path))
            with console.status("[cyan]Indexing into Qdrant..."):
                upsert_file(
                    cfg["qdrant_url"],
                    cfg.get("embed_ollama_url", cfg["ollama_url"]),
                    cfg.get("embed_model", "nomic-embed-text"),
                    rel_path,
                    path.read_text(),
                )
            console.print("[green]✓ Indexed — searchable now via `sre runbook`.[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠ Qdrant index failed ({e}). Will be picked up on next vault sync.[/yellow]")
