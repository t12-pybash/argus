import re
import yaml
import httpx
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich import box

from argus.clients.ollama import OllamaClient
from argus.clients.gitlab import GitLabClient
from argus import config as cfg_module

console = Console()


def _post_deploy_slack(cfg: dict, intent: str, service_name: str, mr_url: str, mr_iid: int):
    bot_token = cfg.get("slack_bot_token", "")
    webhook_url = cfg.get("agent_webhook_url", "")
    if not bot_token and not webhook_url:
        return

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🚀 Argus deploy: GitOps MR opened", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Service:*\n`{service_name}`"},
                {"type": "mrkdwn", "text": f"*MR:*\n<{mr_url}|!{mr_iid} — review manifests →>"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Intent:*\n_{intent}_"},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review MR →", "emoji": True},
                    "style": "primary",
                    "url": mr_url,
                },
            ],
        },
    ]

    if not bot_token:
        return
    try:
        from slack_sdk import WebClient
        channel = cfg.get("agent_mr_channel", "#pull-and-merge-requests")
        WebClient(token=bot_token).chat_postMessage(channel=channel, blocks=blocks, text="Argus deploy: GitOps MR opened")
    except Exception as e:
        console.print(f"[yellow]⚠ Slack notification failed: {e}[/yellow]")


_EXAMPLE_HELMRELEASE = """
--- namespace.yaml ---
apiVersion: v1
kind: Namespace
metadata:
  name: redis

--- helmrelease.yaml ---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: redis
  namespace: redis
spec:
  interval: 30m
  chart:
    spec:
      chart: redis
      version: ">=19.0.0 <20.0.0"
      sourceRef:
        kind: HelmRepository
        name: bitnami
        namespace: flux-system
  values:
    auth:
      enabled: false
    master:
      persistence:
        size: 2Gi

--- kustomization.yaml ---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - helmrelease.yaml
"""

_EXAMPLE_DEPLOYMENT = """
--- namespace.yaml ---
apiVersion: v1
kind: Namespace
metadata:
  name: whoami

--- deployment.yaml ---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: whoami
  namespace: whoami
spec:
  replicas: 1
  selector:
    matchLabels:
      app: whoami
  template:
    metadata:
      labels:
        app: whoami
    spec:
      containers:
        - name: whoami
          image: traefik/whoami:latest
          ports:
            - containerPort: 80
          resources:
            requests:
              cpu: 50m
              memory: 32Mi
---
apiVersion: v1
kind: Service
metadata:
  name: whoami
  namespace: whoami
spec:
  selector:
    app: whoami
  ports:
    - port: 80
      targetPort: 80
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: whoami
  namespace: whoami
spec:
  parentRefs:
    - name: homelab-gw
      namespace: kube-system
  hostnames:
    - whoami.home.t12.io
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: whoami
          port: 80

--- kustomization.yaml ---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - deployment.yaml
"""

_SECTION_RE = re.compile(r"---\s+(\S+\.yaml)\s+---\s*\n(.*?)(?=\n---\s+\S+\.yaml\s+---|---\s+patches\s+---|$)", re.DOTALL)
_PATCHES_RE = re.compile(r"---\s+patches\s+---\s*\n(.*?)(?=\n---\s+\S+|$)", re.DOTALL)


def _add_resource_to_kustomization(raw: str, service_name: str) -> str:
    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return raw
    resources = doc.setdefault("resources", [])
    entry = f"{service_name}/"
    if entry not in resources:
        resources.append(entry)
    return yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True, width=200)


def _merge_kustomization(existing_raw: str, new_raw: str) -> str:
    """Merge resources from new kustomization into existing, avoiding duplicates."""
    try:
        existing = yaml.safe_load(existing_raw) or {}
        new = yaml.safe_load(new_raw) or {}
    except yaml.YAMLError:
        return existing_raw
    existing_resources = existing.setdefault("resources", [])
    for r in new.get("resources", []):
        if r not in existing_resources:
            existing_resources.append(r)
    return yaml.dump(existing, default_flow_style=False, sort_keys=False, allow_unicode=True, width=200)


def _clean_yaml(content: str) -> str:
    lines = content.splitlines()
    cleaned = [line for line in lines if not line.strip().startswith("```")]
    text = "\n".join(cleaned).strip()
    yaml_end = len(text)
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("-") \
                and not stripped.startswith("apiVersion") and ":" not in stripped \
                and not stripped.startswith("---") and len(stripped) > 40:
            yaml_end = len("\n".join(text.splitlines()[:i]))
            break
    return text[:yaml_end].strip()


def _parse_sections(text: str) -> dict[str, str]:
    return {m.group(1): _clean_yaml(m.group(2)) for m in _SECTION_RE.finditer(text)}


def _extract_yaml_blocks(text: str) -> dict[str, str]:
    result = {}
    for m in re.finditer(r"#\s*(\S+\.yaml)\s*\n```(?:yaml)?\s*\n(.*?)```", text, re.DOTALL):
        result[m.group(1)] = m.group(2).strip()
    return result


def _parse_patches(text: str) -> list[dict]:
    m = _PATCHES_RE.search(text)
    if not m:
        return []
    try:
        result = yaml.safe_load(m.group(1).strip())
        if isinstance(result, list):
            return [p for p in result if isinstance(p, dict) and "file" in p and "change" in p]
    except yaml.YAMLError:
        pass
    return []


def _validate_yaml(content: str) -> bool:
    try:
        list(yaml.safe_load_all(content))
        return True
    except yaml.YAMLError:
        return False


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", text.lower().strip())[:40].strip("-")


def _apply_patch(ollama: OllamaClient, file_path: str, existing_content: str, change: str) -> str | None:
    prompt = f"""You are a Kubernetes platform engineer.

Here is the current content of {file_path}:

{existing_content}

Apply exactly this change and output the complete updated file with no other modifications:

{change}

Rules:
- Output ONLY valid Kubernetes YAML
- Do not include comments, explanation, or markdown fences
- Preserve all existing fields not mentioned in the change"""

    raw = ollama.ask(prompt)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```")).strip()
    return raw if _validate_yaml(raw) else None


def deploy(intent: str, dry_run: bool = False):
    cfg = cfg_module.load()

    console.print(f"\n[bold cyan]Generating deployment manifests for:[/bold cyan] {intent}\n")

    intent_lower = intent.lower()
    _wants_raw = any(w in intent_lower for w in ["deployment", "httproute", "service", "raw manifest", "not helmrelease", "no helmrelease"])
    _example = _EXAMPLE_DEPLOYMENT if _wants_raw else _EXAMPLE_HELMRELEASE

    prompt = f"""You are a Kubernetes platform engineer using Flux CD for GitOps.

Generate deployment manifests for the following request:

"{intent}"

Cluster conventions:
- GitOps tool: Flux CD v2 (HelmRelease, Kustomization)
- Every app gets a dedicated namespace
- HelmRepository sources are in the flux-system namespace
- Use HelmRelease ONLY for well-known stateful services with production Helm charts (redis, postgres, mysql, elasticsearch, grafana). Use Deployment + Service + HTTPRoute for everything else — simple services, test apps, or when the request mentions Deployment, HTTPRoute, or a container image directly.
- Gateway API is Cilium: parentRef name=homelab-gw namespace=kube-system; domain suffix is home.t12.io
- Persistent storage uses default StorageClass (leave storageClass: "" or omit it)
- Resource requests: cpu 50m–500m, memory 32Mi–512Mi depending on service size

Reference example (follow this exact pattern):
{_example}

Generate exactly these files for the requested deployment — name each file after what it contains (deployment.yaml, helmrelease.yaml, etc.):

--- namespace.yaml ---
[namespace manifest]

--- deployment.yaml OR helmrelease.yaml ---
[workload manifest — Deployment+Service+HTTPRoute for simple services, HelmRelease for stateful infra]

--- kustomization.yaml ---
[kustomization manifest listing all generated files]

If the request implies that an existing service needs to be reconfigured to use the new deployment
(for example, enabling a cache, changing a backend URL, or pointing to a new service), also output
a patches section listing each existing file that needs updating and what change to make:

--- patches ---
- file: apps/prod/<namespace>/<filename>.yaml
  change: |
    <precise description of what to add or change, including the exact YAML snippet>

Only include the patches section if cross-service changes are actually needed.
Omit it entirely if this is a standalone deployment.

Rules:
- Output ONLY valid Kubernetes YAML
- Follow the --- filename.yaml --- section format exactly
- Every manifest must include apiVersion, kind, metadata.name, metadata.namespace
- Use realistic chart versions (>=X.Y.Z <(X+1).0.0 semver ranges)
- chart.spec.chart must be the bare chart name with no prefix (e.g. "redis" not "bitnami/redis" not "bitnami-redis") — the repo is specified by sourceRef, never include the repo name in the chart field
- HelmRepository sourceRef must point to a real public Helm repo (bitnami, prometheus-community, etc.)
- Do not include comments or explanation outside the YAML blocks and patches section"""

    # Fetch existing file tree so the LLM can reference real paths for cross-service patches
    file_tree_snippet = ""
    if cfg.get("gitlab_token") and cfg.get("gitlab_project_id"):
        try:
            _gl_pre = GitLabClient(cfg["gitlab_url"], cfg["gitlab_token"], cfg["gitlab_project_id"])
            _target_pre = cfg.get("gitlab_target_branch", "main")
            _files = _gl_pre.list_files("apps/prod", ref=_target_pre)
            if _files:
                file_tree_snippet = "\n\nExisting files in apps/prod (use exact paths when referencing files to patch):\n" + "\n".join(f"  {f}" for f in _files)
        except Exception:
            pass

    prompt += file_tree_snippet

    model = cfg.get("diagnose_model", "mistral-nemo")
    ollama = OllamaClient(cfg["ollama_url"], model, cfg.get("ollama_api_key", ""))

    console.print(f"[dim]Generating manifests with {model}…[/dim]")
    raw = ollama.ask(prompt)

    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    files = _parse_sections(raw)
    if not files:
        files = _extract_yaml_blocks(raw)

    if not files:
        console.print("[yellow]⚠ Could not parse structured output from LLM — showing raw:[/yellow]")
        console.print(Panel(raw, title="[yellow]Raw LLM output[/yellow]", box=box.ROUNDED))
        return

    patches = _parse_patches(raw)

    valid = True
    for fname, content in files.items():
        if not _validate_yaml(content):
            console.print(f"[red]✗ Invalid YAML in {fname}[/red]")
            valid = False

    if not valid:
        for fname, content in files.items():
            console.print(Panel(content, title=f"[yellow]{fname}[/yellow]", box=box.ROUNDED, expand=False))
        return

    for fname, content in files.items():
        console.print(Panel(content, title=f"[green]{fname}[/green]", box=box.ROUNDED, expand=False))

    if patches:
        console.print(f"\n[bold cyan]Cross-service patches planned:[/bold cyan]")
        for p in patches:
            console.print(f"  [dim]→[/dim] [cyan]{p['file']}[/cyan]")
            console.print(f"    {p['change'].strip().splitlines()[0]}")

    if dry_run:
        console.print("\n[dim]--dry-run: GitLab MR not created.[/dim]")
        return

    if not cfg.get("gitlab_token"):
        console.print("[red]gitlab_token not configured — set it in config.toml or ARGUS_GITLAB_TOKEN env var.[/red]")
        return

    if not cfg.get("gitlab_project_id"):
        console.print("[red]gitlab_project_id not configured — set it in config.toml or ARGUS_GITLAB_PROJECT_ID env var.[/red]")
        return

    service_name = "service"
    if "namespace.yaml" in files:
        try:
            ns_doc = yaml.safe_load(files["namespace.yaml"])
            service_name = ns_doc.get("metadata", {}).get("name", "") or _slugify(intent)
        except Exception:
            service_name = _slugify(intent)
    else:
        service_name = _slugify(intent)

    gl = GitLabClient(cfg["gitlab_url"], cfg["gitlab_token"], cfg["gitlab_project_id"])
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"argus/deploy/{ts}-{service_name}"
    target = cfg.get("gitlab_target_branch", "main")
    base_path = f"apps/prod/{service_name}"

    commit_files = []
    for fname, content in files.items():
        fpath = f"{base_path}/{fname}"
        existing = gl.get_file(fpath, ref=target)
        if existing and fname == "kustomization.yaml":
            content = _merge_kustomization(existing, content)
        commit_files.append({
            "file_path": fpath,
            "content": content,
            "action": "update" if existing else "create",
        })

    # Update apps/prod/kustomization.yaml to include the new service directory
    parent_kz_path = "apps/prod/kustomization.yaml"
    parent_kz_raw = gl.get_file(parent_kz_path, ref=target)
    if parent_kz_raw:
        updated_parent_kz = _add_resource_to_kustomization(parent_kz_raw, service_name)
        commit_files.append({
            "file_path": parent_kz_path,
            "content": updated_parent_kz,
            "action": "update",
        })

    # Apply cross-service patches
    patched_files = []
    if patches:
        with console.status("[cyan]Applying cross-service patches…"):
            for p in patches:
                existing = gl.get_file(p["file"], ref=target)
                if not existing:
                    console.print(f"[yellow]⚠ Could not read {p['file']} — skipping patch[/yellow]")
                    continue
                updated = _apply_patch(ollama, p["file"], existing, p["change"])
                if updated:
                    commit_files.append({
                        "file_path": p["file"],
                        "content": updated,
                        "action": "update",
                    })
                    patched_files.append(p["file"])
                else:
                    console.print(f"[yellow]⚠ Patch for {p['file']} produced invalid YAML — skipping[/yellow]")

    mr_description = (
        f"Auto-generated by `argus deploy \"{intent}\"`\n\n"
        f"**Service:** `{service_name}`\n"
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"**Files created:**\n"
        + "".join(f"- `{base_path}/{f}`\n" for f in files)
        + (f"\n- `{parent_kz_path}` (updated)" if parent_kz_raw else f"\n> ⚠️ Could not read `{parent_kz_path}` — add `- {service_name}/` manually")
        + ("\n\n**Cross-service patches:**\n" + "".join(f"- `{f}`\n" for f in patched_files) if patched_files else "")
    )

    try:
        with console.status("[cyan]Creating GitLab branch..."):
            gl.create_branch(branch, ref=target)

        with console.status("[cyan]Committing manifests..."):
            gl.commit_files(
                branch=branch,
                message=f"feat(argus): deploy {service_name}",
                files=commit_files,
            )

        with console.status("[cyan]Opening MR..."):
            mr = gl.create_mr(
                source=branch,
                target=target,
                title=f"feat(argus): deploy {service_name}",
                description=mr_description,
            )

        console.print(f"\n[bold green]✓ MR opened:[/bold green] {mr['web_url']}")
        console.print(f"[dim]Branch: {branch}[/dim]")
        _post_deploy_slack(cfg, intent, service_name, mr["web_url"], mr["iid"])

    except Exception as e:
        console.print(f"[red]GitLab error: {e}[/red]")
