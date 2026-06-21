import json
import os
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich import box

from argus.clients.alertmanager import AlertmanagerClient
from argus.clients.kubernetes import pod_describe, pod_logs_previous, pod_events
from argus.clients.ollama import OllamaClient
from argus import config as cfg_module

console = Console()

SEEN_FILE = Path.home() / ".config" / "sre" / "agent_seen.json"
PENDING_FILE = Path.home() / ".config" / "sre" / "agent_pending.json"
PID_FILE = Path.home() / ".config" / "sre" / "agent.pid"
APPROVE_FILE = Path.home() / ".config" / "sre" / "agent_approve"
REJECT_FILE = Path.home() / ".config" / "sre" / "agent_reject"

_shutdown = False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def _save_seen(seen: set):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(list(seen)))


def _slack_payload(title: str, alertname: str, severity: str, pod: str, summary: str, plan: str) -> dict:
    clean_plan = plan.replace("```bash", "```").replace("```shell", "```")
    clean_plan = clean_plan.replace("**", "")
    return {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title, "emoji": True},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Alert:*\n{alertname}"},
                    {"type": "mrkdwn", "text": f"*Severity:*\n{severity}"},
                    {"type": "mrkdwn", "text": f"*Pod:*\n{pod or 'N/A'}"},
                    {"type": "mrkdwn", "text": f"*Summary:*\n{summary}"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Remediation Plan:*\n{clean_plan[:2900]}"},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "Run `argus agent approve` to execute · `argus agent reject` to skip"}
                ],
            },
        ]
    }


def _notify(cfg: dict, title: str, body: str, alert_meta: dict | None = None):
    webhook_url = cfg.get("agent_webhook_url", "")
    if webhook_url:
        try:
            if "discord.com" in webhook_url:
                payload = {"content": f"**{title}**\n{body}", "username": "sre-agent"}
            elif "ntfy.sh" in webhook_url or cfg.get("agent_webhook_type") == "ntfy":
                httpx.post(webhook_url, content=body, headers={"Title": title}, timeout=10)
                payload = None
            elif "hooks.slack.com" in webhook_url and alert_meta:
                payload = _slack_payload(
                    title=title,
                    alertname=alert_meta["alertname"],
                    severity=alert_meta["severity"],
                    pod=alert_meta["pod"],
                    summary=alert_meta["summary"],
                    plan=alert_meta["plan"],
                )
            else:
                payload = {"text": f"*{title}*\n{body}", "username": "sre-agent", "icon_emoji": ":robot_face:"}
            if payload is not None:
                httpx.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            console.print(f"[yellow]⚠ Webhook failed: {e}[/yellow]")
    console.print(Panel(body, title=f"[bold]{title}[/bold]", box=box.ROUNDED, expand=False))


def _embed(text: str, ollama_url: str, model: str) -> list[float]:
    resp = httpx.post(
        f"{ollama_url}/api/embed",
        json={"model": model, "input": text},
        verify=False,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


def _search_qdrant(vec: list[float], qdrant_url: str, limit: int = 3) -> list[dict]:
    resp = httpx.post(
        f"{qdrant_url}/collections/vault/points/search",
        json={"vector": vec, "limit": limit, "with_payload": True},
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def _safe_restart(namespace: str, pod: str) -> str:
    """Restart the deployment owning this pod via kubectl rollout restart."""
    try:
        rs = subprocess.run(
            ["kubectl", "get", "pod", pod, "-n", namespace, "-o",
             "jsonpath={.metadata.ownerReferences[0].name}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not rs:
            return f"Could not determine owner of {namespace}/{pod}"

        deployment = subprocess.run(
            ["kubectl", "get", "replicaset", rs, "-n", namespace, "-o",
             "jsonpath={.metadata.ownerReferences[0].name}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or rs

        result = subprocess.run(
            ["kubectl", "rollout", "restart", f"deployment/{deployment}", "-n", namespace],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return f"Restarted deployment/{deployment} in {namespace}"
        return f"Restart failed: {result.stderr.strip()}"
    except Exception as e:
        return f"Safe restart failed: {e}"


def _process_alert(cfg: dict, alert: dict, auto: bool):
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alertname = labels.get("alertname", "Unknown")
    namespace = labels.get("namespace", "")
    pod = labels.get("pod", "")
    severity = labels.get("severity", "warning")
    summary = annotations.get("summary", "")
    pod_ref = f"{namespace}/{pod}" if namespace and pod else ""

    console.print(f"\n[bold red]🚨 New alert:[/bold red] {alertname} [{severity}]")
    if summary:
        console.print(f"[dim]{summary}[/dim]")

    ollama = OllamaClient(cfg["ollama_url"], cfg["ollama_model"], cfg.get("ollama_api_key", ""))
    embed_url = cfg.get("embed_ollama_url", cfg["ollama_url"])
    embed_model = cfg.get("embed_model", "nomic-embed-text")

    # Diagnose pod if alert labels include one
    diagnosis = ""
    if pod_ref:
        console.print(f"[cyan]Diagnosing {pod_ref}...[/cyan]")
        with ThreadPoolExecutor(max_workers=3) as executor:
            f_describe = executor.submit(pod_describe, namespace, pod)
            f_logs = executor.submit(pod_logs_previous, namespace, pod, 50)
            f_events = executor.submit(pod_events, namespace, pod)
        describe = f_describe.result()
        logs = f_logs.result()
        events = f_events.result()

        if describe:
            diag_prompt = f"""You are a Kubernetes SRE. A {alertname} alert fired for pod {pod_ref}.

kubectl describe (trimmed to 3000 chars):
{describe[:3000]}

Previous logs (last 50 lines):
{logs or "(none)"}

Recent events:
{events or "(none)"}

Alert: {alertname} [{severity}] — {summary}

1. Root cause: quote the specific log line or event that shows the problem.
2. Fix steps: exact kubectl commands to resolve it.
3. Safe to auto-remediate via deployment restart? Answer yes/no and why.

Be concise and specific. No generic Kubernetes advice."""
            with console.status("[cyan]Diagnosing pod..."):
                diagnosis = ollama.ask(diag_prompt)
            console.print("\n")
            console.print(Markdown(diagnosis))

    # Search Qdrant for similar past incidents
    past_context = ""
    try:
        search_query = f"{alertname} {summary} {pod_ref}".strip()
        with console.status("[cyan]Searching past incidents..."):
            vec = _embed(search_query, embed_url, embed_model)
            hits = _search_qdrant(vec, cfg["qdrant_url"])
        past_context = "\n\n".join(
            f"[{h['payload']['file']}]\n{h['payload']['text']}"
            for h in hits if h["score"] >= 0.60
        )
        if past_context:
            console.print(f"[dim]Found {len(hits)} relevant past incidents in vault.[/dim]")
    except Exception as e:
        console.print(f"[yellow]⚠ Qdrant search failed: {e}[/yellow]")

    # Synthesize remediation plan
    diag_section = f"Pod diagnosis:\n{diagnosis}" if diagnosis else "No pod-level diagnosis available."
    past_section = f"Past similar incidents from runbook vault:\n{past_context}" if past_context else "No similar past incidents found."
    remediation_prompt = f"""You are an autonomous SRE agent. An alert fired and you have gathered evidence.

Alert: {alertname} [{severity}]
Summary: {summary}
Pod: {pod_ref or "N/A"}

{diag_section}

{past_section}

Draft a remediation plan:
1. Immediate action — exact kubectl command(s) to run right now
2. Validation — how to confirm the issue is resolved
3. Risk assessment — is a pod restart safe here? Will it cause downtime?

Be direct and specific. Commands only, no background theory."""

    with console.status("[cyan]Drafting remediation plan..."):
        plan = ollama.ask(remediation_prompt)

    console.print("\n[bold cyan]Remediation Plan:[/bold cyan]")
    console.print(Markdown(plan))

    _notify(
        cfg,
        f"🚨 SRE Agent: {alertname} [{severity}]",
        f"Pod: {pod_ref or 'N/A'}\nSummary: {summary}\n\nRemediation Plan:\n{plan}",
        alert_meta={"alertname": alertname, "severity": severity, "pod": pod_ref, "summary": summary, "plan": plan},
    )

    pending = {
        "fingerprint": alert.get("fingerprint", ""),
        "alertname": alertname,
        "namespace": namespace,
        "pod": pod,
        "severity": severity,
        "summary": summary,
        "plan": plan,
        "timestamp": _now(),
    }
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(pending, indent=2))

    if auto:
        console.print("[bold yellow]⚡ Auto mode: executing safe remediation...[/bold yellow]")
        if namespace and pod:
            msg = _safe_restart(namespace, pod)
            console.print(f"[green]✓ {msg}[/green]")
            _notify(cfg, "✅ SRE Agent: Auto-remediated", msg)
        else:
            console.print("[yellow]No pod in alert labels — cannot auto-restart.[/yellow]")
        PENDING_FILE.unlink(missing_ok=True)
    else:
        approval_timeout = int(cfg.get("agent_approval_timeout", "300"))
        console.print(
            f"\nRun [cyan]argus agent approve[/cyan] to restart the pod "
            f"or [red]argus agent reject[/red] to skip. "
            f"(Timeout: {approval_timeout}s)"
        )
        _wait_for_approval(cfg, pending, timeout=approval_timeout)


def _wait_for_approval(cfg: dict, pending: dict, timeout: int = 300):
    APPROVE_FILE.unlink(missing_ok=True)
    REJECT_FILE.unlink(missing_ok=True)

    namespace = pending["namespace"]
    pod = pending["pod"]
    deadline = time.time() + timeout

    while time.time() < deadline and not _shutdown:
        if APPROVE_FILE.exists():
            APPROVE_FILE.unlink(missing_ok=True)
            console.print("[bold green]✅ Approved — executing remediation...[/bold green]")
            if namespace and pod:
                msg = _safe_restart(namespace, pod)
                console.print(f"[green]✓ {msg}[/green]")
                _notify(cfg, "✅ SRE Agent: Remediation executed", msg)
            else:
                console.print("[yellow]No pod to restart.[/yellow]")
            PENDING_FILE.unlink(missing_ok=True)
            return
        if REJECT_FILE.exists():
            REJECT_FILE.unlink(missing_ok=True)
            console.print("[yellow]⏭ Rejected — skipping remediation.[/yellow]")
            PENDING_FILE.unlink(missing_ok=True)
            return
        time.sleep(5)

    console.print(f"[yellow]⏱ Approval timeout ({timeout}s) — skipping.[/yellow]")
    PENDING_FILE.unlink(missing_ok=True)


def start(auto: bool = False, interval: int = 30, namespaces: str = ""):
    global _shutdown
    _shutdown = False

    def _handle_signal(sig, frame):
        global _shutdown
        _shutdown = True
        console.print("\n[yellow]Shutting down argus agent...[/yellow]")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    cfg = cfg_module.load()
    watch_namespaces = {n.strip() for n in namespaces.split(",") if n.strip()}

    console.print(f"[bold green]argus agent started[/bold green] (pid={os.getpid()})")
    console.print(
        f"[dim]Poll: {interval}s | Auto: {auto} | "
        f"Namespaces: {', '.join(watch_namespaces) if watch_namespaces else 'all'}[/dim]"
    )

    am = AlertmanagerClient(cfg["alertmanager_url"])
    seen = _load_seen()

    try:
        while not _shutdown:
            try:
                all_alerts = am.alerts()
                firing = [
                    a for a in all_alerts
                    if a.get("status", {}).get("state") == "active"
                    and a.get("labels", {}).get("alertname") != "Watchdog"
                ]
                if watch_namespaces:
                    firing = [
                        a for a in firing
                        if a.get("labels", {}).get("namespace", "") in watch_namespaces
                    ]

                # Drop fingerprints for alerts that have resolved so re-fires are caught
                firing_fps = {a.get("fingerprint", "") for a in firing}
                seen = seen & firing_fps
                _save_seen(seen)

                new_alerts = [a for a in firing if a.get("fingerprint", "") not in seen]
                for alert in new_alerts:
                    fp = alert.get("fingerprint", "")
                    seen.add(fp)
                    _save_seen(seen)
                    _process_alert(cfg, alert, auto=auto)

                if not new_alerts:
                    console.print(
                        f"[dim]{_now()} — {len(firing)} active alert(s), nothing new[/dim]",
                        end="\r",
                    )

            except Exception as e:
                console.print(f"[red]Agent loop error: {e}[/red]")

            for _ in range(interval):
                if _shutdown:
                    break
                time.sleep(1)
    finally:
        PID_FILE.unlink(missing_ok=True)
        console.print("\n[green]argus agent stopped.[/green]")


def approve():
    if not PENDING_FILE.exists():
        console.print("[yellow]No pending action.[/yellow]")
        return
    pending = json.loads(PENDING_FILE.read_text())
    console.print(
        f"[bold green]Approving:[/bold green] {pending['alertname']} "
        f"({pending.get('namespace', '?')}/{pending.get('pod', '?')})"
    )
    APPROVE_FILE.touch()
    console.print("[green]✓ Approval sent to agent.[/green]")


def reject():
    if not PENDING_FILE.exists():
        console.print("[yellow]No pending action.[/yellow]")
        return
    pending = json.loads(PENDING_FILE.read_text())
    console.print(f"[yellow]Rejecting:[/yellow] {pending['alertname']}")
    REJECT_FILE.touch()
    console.print("[yellow]✓ Rejection sent to agent.[/yellow]")


def status():
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        console.print(f"[bold green]argus agent running[/bold green] (pid={pid})")
    else:
        console.print("[dim]argus agent not running[/dim]")

    if PENDING_FILE.exists():
        pending = json.loads(PENDING_FILE.read_text())
        console.print("\n[bold yellow]Pending action:[/bold yellow]")
        console.print(f"  Alert:  {pending['alertname']} [{pending['severity']}]")
        console.print(f"  Pod:    {pending.get('namespace', '?')}/{pending.get('pod', '?')}")
        console.print(f"  At:     {pending['timestamp']}")
        console.print("\n  Run [cyan]argus agent approve[/cyan] or [red]argus agent reject[/red]")
    else:
        console.print("[dim]No pending action[/dim]")
