import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
import yaml
from rich.console import Console
from rich.panel import Panel
from rich import box

from argus.clients.kubernetes import pod_describe, pod_logs_previous, pod_events, pod_node, namespace_secrets
from argus.clients.ollama import OllamaClient
from argus.clients.gitlab import GitLabClient
from argus import config as cfg_module

try:
    from argus.clients.qdrant import search_vault
    _qdrant = True
except ImportError:
    _qdrant = False

console = Console()


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_yaml(text: str) -> str:
    """Pull YAML content from LLM response — handles ```yaml fences or bare YAML."""
    fenced = re.findall(r"```(?:yaml)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        return "\n---\n".join(f.strip() for f in fenced)
    # Bare output — strip any leading prose before the first ---
    match = re.search(r"^(---\s*\n.+)", text, re.DOTALL | re.MULTILINE)
    if match:
        return match.group(1).strip()
    return text.strip()


def _truncate(text: str, max_lines: int) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-max_lines:]) if len(lines) > max_lines else text.strip()


def _failure_signal(events: str, logs: str) -> str:
    keywords = ["OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "BackOff",
                "Error", "Failed", "Evicted", "Unhealthy", "OOM"]
    found = [kw for kw in keywords if kw.lower() in events.lower() or kw.lower() in logs.lower()]
    log_head = " ".join(logs.strip().splitlines()[:3]) if logs else ""
    return f"{' '.join(found)} {log_head}"[:300].strip()


def _validate_yaml(content: str) -> bool:
    try:
        list(yaml.safe_load_all(content))
        return True
    except yaml.YAMLError:
        return False


def _post_fix_slack(cfg: dict, pod_ref: str, mr_url: str, mr_iid: int, yaml_content: str):
    """Post a Slack notification with the MR link and approve/reject buttons."""
    bot_token = cfg.get("slack_bot_token", "")
    webhook_url = cfg.get("agent_webhook_url", "")
    if not bot_token and not webhook_url:
        return

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🔧 Argus fix: GitOps MR opened", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Pod:*\n`{pod_ref}`"},
                {"type": "mrkdwn", "text": f"*MR:*\n<{mr_url}|!{mr_iid} — review patch →>"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```yaml\n{yaml_content[:1500]}\n```"},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve & Merge", "emoji": True},
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
        },
    ]

    if bot_token:
        try:
            from slack_sdk import WebClient
            channel = cfg.get("slack_channel", "#homelab-alerts")
            WebClient(token=bot_token).chat_postMessage(channel=channel, blocks=blocks)
            return
        except Exception as e:
            console.print(f"[yellow]⚠ Slack notification failed: {e}[/yellow]")

    if webhook_url and "hooks.slack.com" in webhook_url:
        try:
            httpx.post(webhook_url, json={"blocks": blocks}, timeout=10)
        except Exception as e:
            console.print(f"[yellow]⚠ Slack webhook failed: {e}[/yellow]")


def fix(pod_ref: str, dry_run: bool = False, notify_slack: bool = True):
    cfg = cfg_module.load()

    if "/" in pod_ref:
        namespace, pod = pod_ref.split("/", 1)
    else:
        namespace, pod = "default", pod_ref

    console.print(f"\n[bold cyan]Generating GitOps fix for[/bold cyan] {namespace}/{pod}\n")

    with console.status("[cyan]Gathering pod data..."):
        with ThreadPoolExecutor(max_workers=5) as executor:
            f_describe = executor.submit(pod_describe, namespace, pod)
            f_logs = executor.submit(pod_logs_previous, namespace, pod, 50)
            f_events = executor.submit(pod_events, namespace, pod)
            f_node = executor.submit(pod_node, namespace, pod)
            f_secrets = executor.submit(namespace_secrets, namespace)
        describe = f_describe.result()
        logs = f_logs.result()
        events = f_events.result()
        node = f_node.result()
        secrets = f_secrets.result()

    if not describe:
        console.print(f"[red]Pod {namespace}/{pod} not found.[/red]")
        return

    console.print(Panel(
        _truncate(events, 15) or "(no events)",
        title="[yellow]Recent Events[/yellow]",
        box=box.ROUNDED,
        expand=False,
    ))

    # Search vault for relevant runbooks
    vault_context = ""
    signal = _failure_signal(events, logs or "")
    if signal and _qdrant:
        try:
            with console.status("[cyan]Searching vault for relevant runbooks..."):
                hits = search_vault(
                    signal,
                    cfg["qdrant_url"],
                    cfg.get("embed_ollama_url", cfg["ollama_url"]),
                    cfg.get("embed_model", "nomic-embed-text"),
                )
            if hits:
                vault_context = "\n\n---\n\n".join(
                    f"[{h['payload']['file']}]\n{h['payload']['text']}" for h in hits
                )
                console.print(f"[dim]Found {len(hits)} relevant runbook(s) in vault[/dim]")
        except Exception:
            pass

    secrets_list = "\n".join(f"  {s['name']} ({s['type']})" for s in secrets) if secrets else "  (none)"
    runbook_section = f"""--- Relevant runbooks from vault ---
{vault_context}

""" if vault_context else ""

    prompt = f"""You are a Kubernetes SRE. Generate a GitOps Kubernetes manifest to fix a crashing pod.

Pod: {namespace}/{pod}
Node: {node or "(unknown)"}

--- kubectl describe (trimmed) ---
{_truncate(describe, 80)}

--- Previous logs (last 50 lines) ---
{logs or "(none)"}

--- Recent events ---
{events or "(none)"}

--- Secrets in {namespace} ---
{secrets_list}

{runbook_section}Requirements:
- Output ONLY valid Kubernetes YAML — no explanation, no prose
- One or more YAML documents separated by ---
- Every document must include: apiVersion, kind, metadata.name, metadata.namespace
- Use strategic merge patches or full resource replacements that target the specific issue
- The YAML must pass kubeval schema validation
- Do not use kubectl commands or shell scripts — only declarative Kubernetes manifests

If the fix requires a node-level change (e.g. /etc/resolv.conf edit), generate a DaemonSet Job or privileged Pod that makes the change idempotently.

Output ONLY the YAML starting with ---"""

    model = cfg.get("diagnose_model", "mistral-nemo")
    ollama = OllamaClient(cfg["ollama_url"], model, cfg.get("ollama_api_key", ""))

    console.print(f"\n[dim]Generating fix with {model}…[/dim]")
    raw = ollama.ask(prompt)
    raw = _strip_thinking(raw)

    yaml_content = _extract_yaml(raw)

    if not _validate_yaml(yaml_content):
        console.print("[yellow]⚠ LLM output is not valid YAML — showing raw output for review:[/yellow]")
        console.print(Panel(raw, title="[yellow]Raw LLM output[/yellow]", box=box.ROUNDED))
        return

    console.print(Panel(yaml_content, title="[green]Generated Fix (YAML)[/green]", box=box.ROUNDED, expand=False))

    if dry_run:
        console.print("\n[dim]--dry-run: GitLab MR not created.[/dim]")
        return

    if not cfg.get("gitlab_token"):
        console.print("[red]gitlab_token not configured — set it in config.toml or ARGUS_GITLAB_TOKEN env var.[/red]")
        return

    if not cfg.get("gitlab_project_id"):
        console.print("[red]gitlab_project_id not configured — set it in config.toml or ARGUS_GITLAB_PROJECT_ID env var.[/red]")
        return

    # Create GitLab MR
    gl = GitLabClient(cfg["gitlab_url"], cfg["gitlab_token"], cfg["gitlab_project_id"])
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"argus/fix/{ts}-{namespace}-{pod}"[:80]
    file_path = f"patches/{ts}/{namespace}-{pod}-fix.yaml"
    target = cfg.get("gitlab_target_branch", "main")

    mr_url = ""
    mr_iid = None

    try:
        with console.status("[cyan]Creating GitLab branch..."):
            gl.create_branch(branch, ref=target)

        with console.status("[cyan]Committing fix..."):
            gl.commit_files(
                branch=branch,
                message=f"fix(argus): remediate {namespace}/{pod}",
                files=[{"file_path": file_path, "content": yaml_content, "action": "create"}],
            )

        with console.status("[cyan]Opening MR..."):
            mr = gl.create_mr(
                source=branch,
                target=target,
                title=f"fix(argus): remediate {namespace}/{pod}",
                description=f"Auto-generated by `argus fix {pod_ref}`\n\n"
                            f"**Pod:** `{namespace}/{pod}`\n"
                            f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"Review the manifest below before approving.\n\n"
                            f"```yaml\n{yaml_content}\n```",
            )
            mr_url = mr["web_url"]
            mr_iid = mr["iid"]

        console.print(f"\n[bold green]✓ MR opened:[/bold green] {mr_url}")
        console.print(f"[dim]Branch: {branch}[/dim]")
        console.print("\nApprove and merge via GitLab, or run [cyan]argus agent approve[/cyan] if agent is running.")

        if notify_slack:
            _post_fix_slack(cfg, pod_ref, mr_url, mr_iid, yaml_content)

    except Exception as e:
        console.print(f"[red]GitLab error: {e}[/red]")
        console.print("[dim]Generated YAML saved above — apply manually if needed.[/dim]")

    return {"mr_url": mr_url, "mr_iid": mr_iid, "branch": branch}
