import json
import os
import signal
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich import box

from argus.clients.alertmanager import AlertmanagerClient
from argus.clients import kubernetes as k8s
from argus.clients.kubernetes import pod_describe, pod_logs_previous, pod_events
from argus.clients.ollama import OllamaClient
from argus import config as cfg_module

console = Console()

SEEN_FILE = Path.home() / ".config" / "argus" / "agent_seen.json"
PENDING_FILE = Path.home() / ".config" / "argus" / "agent_pending.json"
PID_FILE = Path.home() / ".config" / "argus" / "agent.pid"
APPROVE_FILE = Path.home() / ".config" / "argus" / "agent_approve"
REJECT_FILE = Path.home() / ".config" / "argus" / "agent_reject"

_shutdown = False
_sigterm_count = 0
_approval_event = threading.Event()
_approval_result: str | None = None  # "approve" or "reject"


class _WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global _approval_result
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        result = None

        if self.path == "/slack/actions":
            try:
                raw = urllib.parse.unquote(body)
                # Slack URL verification challenge
                if '"type":"url_verification"' in raw or "url_verification" in raw:
                    try:
                        data = json.loads(raw)
                        challenge = data.get("challenge", "")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"challenge": challenge}).encode())
                        return
                    except Exception:
                        pass
                # Slack interactive component: payload=<url-encoded JSON>
                if raw.startswith("payload="):
                    raw = raw[len("payload="):]
                payload = json.loads(raw)
                action_id = payload.get("actions", [{}])[0].get("action_id", "")
                if action_id in ("approve", "reject"):
                    result = action_id
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"text":"Processing..."}')

        elif self.path in ("/approve", "/reject"):
            result = "approve" if self.path == "/approve" else "reject"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        else:
            self.send_response(404)
            self.end_headers()

        if result:
            _approval_result = result
            _approval_event.set()

    def log_message(self, *_):
        pass


def _start_webhook_server(port: int):
    try:
        server = HTTPServer(("", port), _WebhookHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        console.print(f"[dim]Webhook on :{port} — POST /approve · /reject · /slack/actions[/dim]")
    except OSError as e:
        console.print(f"[yellow]⚠ Webhook server failed to start on :{port}: {e}[/yellow]")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def _save_seen(seen: set):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(list(seen)))


def _slack_payload(title: str, alertname: str, severity: str, pod: str, summary: str, plan: str, action: str = "", interactive: bool = False) -> dict:
    clean_plan = plan.replace("```bash", "```").replace("```shell", "```")
    clean_plan = clean_plan.replace("**", "")
    blocks = [
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
                {"type": "mrkdwn", "text": f"*Action:*\n`{action or 'none'}`"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Remediation Plan:*\n{clean_plan[:2900]}"},
        },
    ]
    if interactive:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "approve",
                    "value": "approve",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "reject",
                    "value": "reject",
                },
            ],
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Run `argus agent approve` to execute · `argus agent reject` to skip"}
            ],
        })
    return {"blocks": blocks}


def _notify(cfg: dict, title: str, body: str, alert_meta: dict | None = None):
    webhook_url = cfg.get("agent_webhook_url", "")
    if webhook_url:
        try:
            if "discord.com" in webhook_url:
                payload = {"content": f"**{title}**\n{body}", "username": "argus-agent"}
            elif "ntfy.sh" in webhook_url or cfg.get("agent_webhook_type") == "ntfy":
                httpx.post(webhook_url, content=body, headers={"Title": title}, timeout=10)
                payload = None
            elif "hooks.slack.com" in webhook_url and alert_meta:
                interactive = bool(cfg.get("agent_interactivity_url", ""))
                payload = _slack_payload(
                    title=title,
                    alertname=alert_meta["alertname"],
                    severity=alert_meta["severity"],
                    pod=alert_meta["pod"],
                    summary=alert_meta["summary"],
                    plan=alert_meta["plan"],
                    action=alert_meta.get("action", ""),
                    interactive=interactive,
                )
            else:
                payload = {"text": f"*{title}*\n{body}", "username": "argus-agent", "icon_emoji": ":robot_face:"}
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


_VALID_ACTIONS = {
    "rollout_restart", "rollout_undo", "force_delete_pod", "scale_deployment",
    "expand_pvc", "delete_failed_jobs", "cordon_node", "drain_node",
    "flux_reconcile", "flux_suspend", "restart_coredns", "none",
}

_ACTION_GUIDE = """
- rollout_restart: pod crashlooping or stuck, deployment exists
- rollout_undo: new rollout caused the alert (OOMKilled/error spike after deploy)
- force_delete_pod: pod stuck in Terminating or Unknown state
- scale_deployment: resource pressure, need more replicas (set replicas field)
- expand_pvc: PVC near capacity — set name to PVC name and size to new capacity e.g. 20Gi
- delete_failed_jobs: failed batch jobs accumulating in namespace
- cordon_node: node unhealthy, stop scheduling new pods onto it (set name to node name)
- drain_node: evacuate all pods from a troubled node (set name to node name)
- flux_reconcile: Flux sync stuck or drifted (set flux_kind to kustomization or helmrelease)
- flux_suspend: HelmRelease looping upgrade failures (set flux_kind to helmrelease)
- restart_coredns: DNS resolution failures cluster-wide
- none: requires human investigation, no safe automated action
"""


def _pick_action(
    ollama: OllamaClient,
    alertname: str,
    severity: str,
    pod_ref: str,
    namespace: str,
    pod: str,
    diagnosis: str,
    summary: str,
) -> dict:
    prompt = f"""You are an SRE agent. Choose exactly ONE remediation action for this alert.

Alert: {alertname} [{severity}]
Summary: {summary}
Pod: {pod_ref or "N/A"}
Namespace: {namespace or "N/A"}

Diagnosis:
{diagnosis[:2000] if diagnosis else "No diagnosis available."}

Action guide:{_ACTION_GUIDE}

Reply with ONLY a valid JSON object, no other text:
{{
  "action": "<action from guide>",
  "namespace": "{namespace or ""}",
  "name": "<deployment, pod, node, or PVC name — empty if not applicable>",
  "replicas": 0,
  "size": "",
  "flux_kind": "",
  "safe_to_auto": false
}}"""

    try:
        raw = ollama.ask(prompt)
        import re as _re
        match = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if parsed.get("action") not in _VALID_ACTIONS:
                parsed["action"] = "none"
            return parsed
    except Exception:
        pass
    return {"action": "none", "namespace": namespace, "name": "", "safe_to_auto": False}


def _execute_action(action_plan: dict, fallback_namespace: str, fallback_pod: str) -> str:
    action = action_plan.get("action", "none")
    ns = action_plan.get("namespace") or fallback_namespace
    name = action_plan.get("name", "")

    try:
        if action == "rollout_restart":
            if not name and fallback_pod:
                name = k8s.get_deployment_for_pod(ns, fallback_pod)
            return k8s.rollout_restart(ns, name) if name else f"No deployment resolved for {ns}/{fallback_pod}"

        if action == "rollout_undo":
            if not name and fallback_pod:
                name = k8s.get_deployment_for_pod(ns, fallback_pod)
            return k8s.rollout_undo(ns, name) if name else f"No deployment resolved for rollback in {ns}"

        if action == "force_delete_pod":
            target = name or fallback_pod
            return k8s.force_delete_pod(ns, target) if target else "No pod identified for force delete"

        if action == "scale_deployment":
            replicas = int(action_plan.get("replicas") or 2)
            if not name and fallback_pod:
                name = k8s.get_deployment_for_pod(ns, fallback_pod)
            return k8s.scale_deployment(ns, name, replicas) if name else f"No deployment resolved for scale in {ns}"

        if action == "expand_pvc":
            size = action_plan.get("size", "")
            if not name or not size:
                return f"expand_pvc requires name and size (got name={name!r}, size={size!r})"
            return k8s.expand_pvc(ns, name, size)

        if action == "delete_failed_jobs":
            return k8s.delete_failed_jobs(ns)

        if action == "cordon_node":
            return k8s.cordon_node(name) if name else "No node name in action plan"

        if action == "drain_node":
            return k8s.drain_node(name) if name else "No node name in action plan"

        if action == "flux_reconcile":
            flux_kind = action_plan.get("flux_kind") or "kustomization"
            return k8s.flux_reconcile(flux_kind, name, ns) if name else "No Flux resource name in action plan"

        if action == "flux_suspend":
            flux_kind = action_plan.get("flux_kind") or "helmrelease"
            return k8s.flux_suspend(flux_kind, name, ns) if name else "No Flux resource name in action plan"

        if action == "restart_coredns":
            return k8s.restart_coredns()

        return "No automated action — human investigation required"
    except Exception as e:
        return f"Action {action} failed: {e}"


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

    with console.status("[cyan]Picking remediation action..."):
        action_plan = _pick_action(ollama, alertname, severity, pod_ref, namespace, pod, diagnosis, summary)

    action_label = action_plan["action"]
    if action_plan.get("name"):
        action_label += f" → {action_plan['name']}"
    console.print(f"\n[bold yellow]Planned action:[/bold yellow] {action_label}")

    _notify(
        cfg,
        f"🚨 SRE Agent: {alertname} [{severity}]",
        f"Pod: {pod_ref or 'N/A'}\nSummary: {summary}\n\nAction: {action_label}\n\nRemediation Plan:\n{plan}",
        alert_meta={
            "alertname": alertname,
            "severity": severity,
            "pod": pod_ref,
            "summary": summary,
            "plan": plan,
            "action": action_label,
        },
    )

    pending = {
        "fingerprint": alert.get("fingerprint", ""),
        "alertname": alertname,
        "namespace": namespace,
        "pod": pod,
        "severity": severity,
        "summary": summary,
        "plan": plan,
        "action_plan": action_plan,
        "timestamp": _now(),
    }
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(pending, indent=2))

    if auto:
        console.print(f"[bold yellow]⚡ Auto mode: executing {action_label}...[/bold yellow]")
        msg = _execute_action(action_plan, namespace, pod)
        console.print(f"[green]✓ {msg}[/green]")
        _notify(cfg, "✅ SRE Agent: Auto-remediated", msg)
        PENDING_FILE.unlink(missing_ok=True)
    else:
        approval_timeout = int(cfg.get("agent_approval_timeout", "300"))
        console.print(
            f"\nRun [cyan]argus agent approve[/cyan] to execute [bold]{action_label}[/bold] "
            f"or [red]argus agent reject[/red] to skip. "
            f"(Timeout: {approval_timeout}s)"
        )
        _wait_for_approval(cfg, pending, timeout=approval_timeout)


def _wait_for_approval(cfg: dict, pending: dict, timeout: int = 300):
    global _approval_result
    _approval_event.clear()
    _approval_result = None
    APPROVE_FILE.unlink(missing_ok=True)
    REJECT_FILE.unlink(missing_ok=True)

    namespace = pending["namespace"]
    pod = pending["pod"]
    action_plan = pending.get("action_plan", {})
    deadline = time.time() + timeout

    while time.time() < deadline and not _shutdown:
        # Check event (set by webhook server or signal handler)
        if _approval_event.wait(timeout=1):
            _approval_event.clear()
            break
        # Legacy file-based fallback
        if APPROVE_FILE.exists():
            APPROVE_FILE.unlink(missing_ok=True)
            _approval_result = "approve"
            break
        if REJECT_FILE.exists():
            REJECT_FILE.unlink(missing_ok=True)
            _approval_result = "reject"
            break

    if _approval_result == "approve":
        console.print("[bold green]✅ Approved — executing remediation...[/bold green]")
        msg = _execute_action(action_plan, namespace, pod)
        console.print(f"[green]✓ {msg}[/green]")
        _notify(cfg, "✅ SRE Agent: Remediation executed", msg)
        PENDING_FILE.unlink(missing_ok=True)
    elif _approval_result == "reject":
        console.print("[yellow]⏭ Rejected — skipping remediation.[/yellow]")
        PENDING_FILE.unlink(missing_ok=True)
    else:
        console.print(f"[yellow]⏱ Approval timeout ({timeout}s) — skipping.[/yellow]")
        PENDING_FILE.unlink(missing_ok=True)


def start(auto: bool = False, interval: int = 30, namespaces: str = ""):
    global _shutdown, _sigterm_count
    _shutdown = False
    _sigterm_count = 0

    def _handle_signal(sig, frame):
        global _shutdown, _sigterm_count
        _sigterm_count += 1
        _shutdown = True
        _approval_event.set()  # immediately unblock any waiting approval loop
        if _sigterm_count > 1:
            console.print("\n[red]Force killing...[/red]")
            os.kill(os.getpid(), signal.SIGKILL)
        console.print("\n[yellow]Shutting down (send again to force kill)...[/yellow]")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    cfg = cfg_module.load()
    watch_namespaces = {n.strip() for n in namespaces.split(",") if n.strip()}

    webhook_port = int(cfg.get("agent_webhook_port", "7654"))
    _start_webhook_server(webhook_port)

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


def kill():
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGKILL)
            PID_FILE.unlink(missing_ok=True)
            console.print(f"[green]✓ Agent killed (PID {pid})[/green]")
        except (ValueError, ProcessLookupError):
            PID_FILE.unlink(missing_ok=True)
            console.print("[yellow]Agent not running (stale PID file removed)[/yellow]")
    else:
        ret = os.system("pkill -9 -f 'argus agent start' 2>/dev/null")
        if ret == 0:
            console.print("[green]✓ Agent killed[/green]")
        else:
            console.print("[yellow]No agent found running[/yellow]")


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
        ap = pending.get("action_plan", {})
        action_label = ap.get("action", "unknown")
        if ap.get("name"):
            action_label += f" → {ap['name']}"
        console.print("\n[bold yellow]Pending action:[/bold yellow]")
        console.print(f"  Alert:  {pending['alertname']} [{pending['severity']}]")
        console.print(f"  Pod:    {pending.get('namespace', '?')}/{pending.get('pod', '?')}")
        console.print(f"  Action: {action_label}")
        console.print(f"  At:     {pending['timestamp']}")
        console.print("\n  Run [cyan]argus agent approve[/cyan] or [red]argus agent reject[/red]")
    else:
        console.print("[dim]No pending action[/dim]")
