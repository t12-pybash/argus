import re
import yaml
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich import box

from argus.clients.ollama import OllamaClient
from argus.clients.gitlab import GitLabClient
from argus import config as cfg_module

console = Console()


# Few-shot example showing the manifest structure used in this cluster
_EXAMPLE_MANIFESTS = """
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

_SECTION_RE = re.compile(r"---\s+(\S+\.yaml)\s+---\s*\n(.*?)(?=\n---\s+\S+\.yaml\s+---|$)", re.DOTALL)


def _clean_yaml(content: str) -> str:
    """Strip markdown code fences and trailing prose from extracted YAML."""
    # Remove ```yaml, ```yml, ``` lines
    lines = content.splitlines()
    cleaned = [l for l in lines if not l.strip().startswith("```")]
    text = "\n".join(cleaned).strip()
    # Truncate at first line of prose after YAML ends (non-YAML lines after last key: value)
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
    """Parse LLM output into {filename: content} dict."""
    return {m.group(1): _clean_yaml(m.group(2)) for m in _SECTION_RE.finditer(text)}


def _extract_yaml_blocks(text: str) -> dict[str, str]:
    """Fallback: extract ```yaml ... ``` fenced blocks labelled with a filename comment."""
    result = {}
    for m in re.finditer(r"#\s*(\S+\.yaml)\s*\n```(?:yaml)?\s*\n(.*?)```", text, re.DOTALL):
        result[m.group(1)] = m.group(2).strip()
    return result


def _validate_yaml(content: str) -> bool:
    try:
        list(yaml.safe_load_all(content))
        return True
    except yaml.YAMLError:
        return False


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", text.lower().strip())[:40].strip("-")


def deploy(intent: str, dry_run: bool = False):
    cfg = cfg_module.load()

    console.print(f"\n[bold cyan]Generating deployment manifests for:[/bold cyan] {intent}\n")

    prompt = f"""You are a Kubernetes platform engineer using Flux CD for GitOps.

Generate deployment manifests for the following request:

"{intent}"

Cluster conventions:
- GitOps tool: Flux CD v2 (HelmRelease, Kustomization)
- Every app gets a dedicated namespace
- HelmRepository sources are in the flux-system namespace
- Use HelmRelease for anything with a public Helm chart; use Deployment + Service for custom images
- Persistent storage uses default StorageClass (leave storageClass: "" or omit it)
- Resource requests: cpu 100m–500m, memory 128Mi–512Mi for small services

Reference example (use this exact structure):
{_EXAMPLE_MANIFESTS}

Generate exactly these three files for the requested deployment:

--- namespace.yaml ---
[namespace manifest]

--- helmrelease.yaml ---
[helmrelease or deployment + service manifest]

--- kustomization.yaml ---
[kustomization manifest]

Rules:
- Output ONLY valid Kubernetes YAML
- Follow the --- filename.yaml --- section format exactly
- Every manifest must include apiVersion, kind, metadata.name, metadata.namespace
- Use realistic chart versions (>=X.Y.Z <(X+1).0.0 semver ranges)
- chart.spec.chart must be the bare chart name with no prefix (e.g. "redis" not "bitnami/redis" not "bitnami-redis") — the repo is specified by sourceRef, never include the repo name in the chart field
- HelmRepository sourceRef must point to a real public Helm repo (bitnami, prometheus-community, etc.)
- Do not include comments or explanation outside the YAML blocks"""

    model = cfg.get("diagnose_model", "mistral-nemo")
    ollama = OllamaClient(cfg["ollama_url"], model, cfg.get("ollama_api_key", ""))

    console.print(f"[dim]Generating manifests with {model}…[/dim]")
    raw = ollama.ask(prompt)

    # Strip <think> blocks
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    files = _parse_sections(raw)
    if not files:
        files = _extract_yaml_blocks(raw)

    if not files:
        console.print("[yellow]⚠ Could not parse structured output from LLM — showing raw:[/yellow]")
        console.print(Panel(raw, title="[yellow]Raw LLM output[/yellow]", box=box.ROUNDED))
        return

    # Validate each file
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

    if dry_run:
        console.print("\n[dim]--dry-run: GitLab MR not created.[/dim]")
        return

    if not cfg.get("gitlab_token"):
        console.print("[red]gitlab_token not configured — set it in config.toml or ARGUS_GITLAB_TOKEN env var.[/red]")
        return

    if not cfg.get("gitlab_project_id"):
        console.print("[red]gitlab_project_id not configured — set it in config.toml or ARGUS_GITLAB_PROJECT_ID env var.[/red]")
        return

    # Infer service name from namespace.yaml if possible
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

    commit_files = [
        {
            "file_path": f"{base_path}/{fname}",
            "content": content,
            "action": "create",
        }
        for fname, content in files.items()
    ]

    # Add entry to apps/prod/kustomization.yaml — create a patch file for the parent kustomization
    parent_patch = f"# Add {service_name} to apps/prod/kustomization.yaml:\n# - {service_name}/\n"
    commit_files.append({
        "file_path": f"{base_path}/.argus-note",
        "content": parent_patch,
        "action": "create",
    })

    mr_description = (
        f"Auto-generated by `argus deploy \"{intent}\"`\n\n"
        f"**Service:** `{service_name}`\n"
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"**Files created:**\n"
        + "".join(f"- `{base_path}/{f}`\n" for f in files)
        + f"\n> ⚠️ Before merging: add `- {service_name}/` to `apps/prod/kustomization.yaml`"
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
        console.print(f"[yellow]⚠ Remember to add `- {service_name}/` to apps/prod/kustomization.yaml before merging[/yellow]")

    except Exception as e:
        console.print(f"[red]GitLab error: {e}[/red]")
