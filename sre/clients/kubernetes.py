import re
import subprocess
from collections import defaultdict


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def running_pods_by_namespace() -> dict[str, list[str]]:
    """Returns {namespace: [app-name, ...]} for all Running pods."""
    output = _run(["kubectl", "get", "pods", "-A", "--field-selector=status.phase=Running",
                   "--no-headers", "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name"])
    ns_pods: dict[str, list[str]] = defaultdict(list)
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ns, pod = parts[0], parts[1]
        # Strip pod hash suffixes (e.g. nextcloud-7d9f4b-xkqzp → nextcloud)
        app = re.sub(r'-[a-f0-9]{8,10}-[a-z0-9]{5}$', '', pod)
        app = re.sub(r'-[a-z0-9]{5}$', '', app)
        if app not in ns_pods[ns]:
            ns_pods[ns].append(app)
    return dict(ns_pods)


def format_pods_context(ns_pods: dict[str, list[str]]) -> str:
    lines = []
    skip = {"kube-system", "kube-public", "kube-node-lease"}
    for ns, apps in sorted(ns_pods.items()):
        if ns in skip:
            continue
        lines.append(f"  {ns}: {', '.join(sorted(apps))}")
    return "Running workloads by namespace:\n" + "\n".join(lines)


def pvcs() -> list[dict]:
    """Returns list of {namespace, name, capacity, status} for all PVCs."""
    output = _run([
        "kubectl", "get", "pvc", "-A", "--no-headers",
        "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,CAP:.status.capacity.storage,PHASE:.status.phase"
    ])
    result = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        result.append({"namespace": parts[0], "name": parts[1], "capacity": parts[2], "phase": parts[3]})
    return result


def format_pvcs_context(pvc_list: list[dict]) -> str:
    if not pvc_list:
        return "PVCs: none"
    lines = [f"  {p['namespace']}/{p['name']}: {p['capacity']} ({p['phase']})" for p in pvc_list]
    return "PersistentVolumeClaims:\n" + "\n".join(lines)


def problem_pods() -> list[dict]:
    """Returns pods that are not Running or Succeeded."""
    output = _run([
        "kubectl", "get", "pods", "-A", "--no-headers",
        "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason"
    ])
    result = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        phase = parts[2]
        if phase not in ("Running", "Succeeded"):
            result.append({"namespace": parts[0], "name": parts[1], "phase": phase,
                           "reason": parts[3] if len(parts) > 3 else ""})
    return result


def format_problem_pods_context(pods: list[dict]) -> str:
    if not pods:
        return "Problem pods: none"
    lines = [f"  {p['namespace']}/{p['name']}: {p['phase']} {p['reason']}".rstrip() for p in pods]
    return "Problem pods (non-Running/Succeeded):\n" + "\n".join(lines)


def deployment_readiness() -> list[dict]:
    """Returns deployments where ready replicas != desired."""
    output = _run([
        "kubectl", "get", "deployments", "-A", "--no-headers",
        "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas"
    ])
    result = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        desired = parts[2]
        ready = parts[3]
        if desired != ready:
            result.append({"namespace": parts[0], "name": parts[1], "desired": desired, "ready": ready})
    return result


def format_deployment_readiness_context(deps: list[dict]) -> str:
    if not deps:
        return "Deployments: all ready"
    lines = [f"  {d['namespace']}/{d['name']}: {d['ready']}/{d['desired']} ready" for d in deps]
    return "Degraded deployments:\n" + "\n".join(lines)


def flux_sync_status() -> list[dict]:
    """Returns Flux Kustomizations and HelmReleases that are not Ready."""
    issues = []
    for kind, group in [("kustomization", "kustomize.toolkit.fluxcd.io"),
                        ("helmrelease", "helm.toolkit.fluxcd.io")]:
        output = _run([
            "kubectl", "get", kind, "-A", "--no-headers",
            "-o", f"custom-columns=NS:.metadata.namespace,NAME:.metadata.name,READY:.status.conditions[0].status,MSG:.status.conditions[0].message"
        ])
        for line in output.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3:
                continue
            ready = parts[2]
            if ready.lower() != "true":
                issues.append({"kind": kind, "namespace": parts[0], "name": parts[1],
                               "message": parts[3][:80] if len(parts) > 3 else ""})
    return issues


def format_flux_context(issues: list[dict]) -> str:
    if not issues:
        return "Flux: all Kustomizations and HelmReleases synced"
    lines = [f"  {i['kind']}/{i['namespace']}/{i['name']}: {i['message']}".rstrip() for i in issues]
    return "Flux sync issues:\n" + "\n".join(lines)
