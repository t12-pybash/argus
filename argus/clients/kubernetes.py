import json
import re
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor


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


def running_and_problem_pods() -> tuple[dict[str, list[str]], list[dict]]:
    """Single kubectl call returning (running_by_namespace, problem_pods)."""
    output = _run([
        "kubectl", "get", "pods", "-A", "--no-headers",
        "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason"
    ])
    running: dict[str, list[str]] = defaultdict(list)
    problems = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        ns, pod, phase = parts[0], parts[1], parts[2]
        reason = parts[3] if len(parts) > 3 else ""
        if phase == "Running":
            app = re.sub(r'-[a-f0-9]{8,10}-[a-z0-9]{5}$', '', pod)
            app = re.sub(r'-[a-z0-9]{5}$', '', app)
            if app not in running[ns]:
                running[ns].append(app)
        elif phase != "Succeeded":
            problems.append({"namespace": ns, "name": pod, "phase": phase, "reason": reason})
    return dict(running), problems


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
    def _fetch(kind: str) -> list[dict]:
        output = _run([
            "kubectl", "get", kind, "-A", "--no-headers",
            "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,READY:.status.conditions[0].status,MSG:.status.conditions[0].message"
        ])
        result = []
        for line in output.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3:
                continue
            if parts[2].lower() != "true":
                result.append({"kind": kind, "namespace": parts[0], "name": parts[1],
                               "message": parts[3][:80] if len(parts) > 3 else ""})
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        k = executor.submit(_fetch, "kustomization")
        h = executor.submit(_fetch, "helmrelease")
    return k.result() + h.result()


def format_flux_context(issues: list[dict]) -> str:
    if not issues:
        return "Flux: all Kustomizations and HelmReleases synced"
    lines = [f"  {i['kind']}/{i['namespace']}/{i['name']}: {i['message']}".rstrip() for i in issues]
    return "Flux sync issues:\n" + "\n".join(lines)


def pod_exit_reason(namespace: str, pod: str) -> str:
    """Returns 'OOMKilled (exit 137)' style string, or empty if no last terminated state."""
    out = _run([
        "kubectl", "get", "pod", pod, "-n", namespace, "-o",
        "jsonpath={.status.containerStatuses[0].lastState.terminated.reason} {.status.containerStatuses[0].lastState.terminated.exitCode}"
    ]).strip()
    if not out or out == " ":
        return ""
    parts = out.split()
    reason = parts[0] if parts else ""
    code = parts[1] if len(parts) > 1 else ""
    if reason and code:
        return f"{reason} (exit {code})"
    return reason


def pod_node(namespace: str, pod: str) -> str:
    return _run(["kubectl", "get", "pod", pod, "-n", namespace, "-o", "jsonpath={.spec.nodeName}"])


def namespace_secrets(namespace: str) -> list[dict]:
    """Returns [{name, type}] for all secrets in the namespace."""
    output = _run([
        "kubectl", "get", "secrets", "-n", namespace, "--no-headers",
        "-o", "custom-columns=NAME:.metadata.name,TYPE:.type"
    ])
    result = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result.append({"name": parts[0], "type": parts[1]})
    return result


def pod_describe(namespace: str, pod: str) -> str:
    return _run(["kubectl", "describe", "pod", pod, "-n", namespace])


def pod_logs_previous(namespace: str, pod: str, tail: int = 50) -> str:
    return _run(["kubectl", "logs", pod, "-n", namespace, "--previous", f"--tail={tail}"])


def pod_events(namespace: str, pod: str) -> str:
    return _run([
        "kubectl", "get", "events", "-n", namespace,
        f"--field-selector=involvedObject.name={pod}",
        "--sort-by=.lastTimestamp"
    ])


def inference_services() -> list[dict]:
    """Returns [{namespace, name, ready, url}] for all KServe InferenceServices.
    Returns empty list if KServe CRDs are not installed."""
    output = _run(["kubectl", "get", "inferenceservice", "-A", "-o", "json"])
    if not output:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    result = []
    for item in data.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        url = item.get("status", {}).get("url", "")
        conditions = item.get("status", {}).get("conditions", [])
        ready = next((c["status"] == "True" for c in conditions if c["type"] == "Ready"), False)
        result.append({"namespace": ns, "name": name, "ready": ready, "url": url})
    return result


def gpu_metrics(namespace: str, isvc_name: str) -> dict | None:
    """Returns GPU utilization % and VRAM usage via nvidia-smi exec into predictor pod."""
    pod = _run([
        "kubectl", "get", "pod", "-n", namespace,
        "-l", f"serving.kserve.io/inferenceservice={isvc_name}",
        "--no-headers", "-o", "custom-columns=NAME:.metadata.name"
    ])
    if not pod:
        return None
    output = _run([
        "kubectl", "exec", pod, "-n", namespace, "--",
        "nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits"
    ])
    if not output:
        return None
    parts = [p.strip() for p in output.split(",")]
    if len(parts) < 3:
        return None
    try:
        return {
            "utilization": int(parts[0]),
            "memory_used_mb": int(parts[1]),
            "memory_total_mb": int(parts[2]),
        }
    except ValueError:
        return None


def ollama_models(namespace: str, isvc_name: str) -> list[dict]:
    """List Ollama models via 'ollama list' exec into the predictor pod."""
    pod = _run([
        "kubectl", "get", "pod", "-n", namespace,
        "-l", f"serving.kserve.io/inferenceservice={isvc_name}",
        "--no-headers", "-o", "custom-columns=NAME:.metadata.name"
    ])
    if not pod:
        return []
    output = _run(["kubectl", "exec", pod, "-n", namespace, "--", "ollama", "list"])
    models = []
    for line in output.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) >= 3:
            models.append({"name": parts[0], "size": f"{parts[2]} {parts[3]}"})
    return models


def inference_service_image(namespace: str, name: str) -> str:
    """Returns the current container image for a KServe InferenceService."""
    return _run([
        "kubectl", "get", "inferenceservice", name, "-n", namespace,
        "-o", "jsonpath={.spec.predictor.containers[0].image}"
    ])


def patch_inference_service_image(namespace: str, name: str, image: str) -> str:
    """Patches the container image of a KServe InferenceService."""
    patch = json.dumps({
        "spec": {"predictor": {"containers": [{"name": "kserve-container", "image": image}]}}
    })
    result = subprocess.run(
        ["kubectl", "patch", "inferenceservice", name, "-n", namespace, "--type=merge", "-p", patch],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"InferenceService {namespace}/{name} updated to {image}"
    return f"Patch failed: {result.stderr.strip()}"


def rollback_inference_service(namespace: str, name: str) -> str:
    """Rollback a KServe InferenceService via its underlying deployment."""
    deployment = _run([
        "kubectl", "get", "deployment", "-n", namespace,
        "-l", f"serving.kserve.io/inferenceservice={name}",
        "--no-headers", "-o", "custom-columns=NAME:.metadata.name"
    ])
    if not deployment:
        return f"No deployment found for InferenceService {name}"
    result = subprocess.run(
        ["kubectl", "rollout", "undo", f"deployment/{deployment}", "-n", namespace],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Rolled back InferenceService {namespace}/{name} via deployment/{deployment}"
    return f"Rollback failed: {result.stderr.strip()}"


# --- Remediation actions ---

def get_deployment_for_pod(namespace: str, pod: str) -> str:
    """Resolve deployment name from pod name via its ReplicaSet owner."""
    rs = _run([
        "kubectl", "get", "pod", pod, "-n", namespace, "-o",
        "jsonpath={.metadata.ownerReferences[0].name}",
    ])
    if not rs:
        return ""
    deployment = _run([
        "kubectl", "get", "replicaset", rs, "-n", namespace, "-o",
        "jsonpath={.metadata.ownerReferences[0].name}",
    ])
    return deployment or rs


def rollout_restart(namespace: str, name: str) -> str:
    for kind in ("deployment", "daemonset", "statefulset"):
        result = subprocess.run(
            ["kubectl", "rollout", "restart", f"{kind}/{name}", "-n", namespace],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return f"Restarted {kind}/{name} in {namespace}"
    return f"Rollout restart failed: {name} not found as deployment, daemonset, or statefulset in {namespace}"


def rollout_undo(namespace: str, deployment: str) -> str:
    result = subprocess.run(
        ["kubectl", "rollout", "undo", f"deployment/{deployment}", "-n", namespace],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Rolled back deployment/{deployment} in {namespace}"
    return f"Rollback failed: {result.stderr.strip()}"


def force_delete_pod(namespace: str, pod: str) -> str:
    result = subprocess.run(
        ["kubectl", "delete", "pod", pod, "-n", namespace, "--grace-period=0", "--force"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Force deleted pod {namespace}/{pod}"
    return f"Force delete failed: {result.stderr.strip()}"


def scale_deployment(namespace: str, deployment: str, replicas: int) -> str:
    result = subprocess.run(
        ["kubectl", "scale", f"deployment/{deployment}", f"--replicas={replicas}", "-n", namespace],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Scaled deployment/{deployment} to {replicas} replica(s) in {namespace}"
    return f"Scale failed: {result.stderr.strip()}"


def expand_pvc(namespace: str, pvc_name: str, new_size: str) -> str:
    patch = json.dumps({"spec": {"resources": {"requests": {"storage": new_size}}}})
    result = subprocess.run(
        ["kubectl", "patch", "pvc", pvc_name, "-n", namespace, "--type=merge", "-p", patch],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Expanded PVC {namespace}/{pvc_name} to {new_size}"
    return f"PVC expand failed: {result.stderr.strip()}"


def delete_failed_jobs(namespace: str) -> str:
    result = subprocess.run(
        ["kubectl", "delete", "jobs", "-n", namespace, "--field-selector=status.successful=0"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Deleted failed jobs in {namespace}"
    return f"Job cleanup failed: {result.stderr.strip()}"


def cordon_node(node: str) -> str:
    result = subprocess.run(
        ["kubectl", "cordon", node],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Cordoned node {node}"
    return f"Cordon failed: {result.stderr.strip()}"


def drain_node(node: str) -> str:
    result = subprocess.run(
        ["kubectl", "drain", node, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=120s"],
        capture_output=True, text=True, timeout=150,
    )
    if result.returncode == 0:
        return f"Drained node {node}"
    return f"Drain failed: {result.stderr.strip()}"


def restart_coredns() -> str:
    result = subprocess.run(
        ["kubectl", "rollout", "restart", "deployment/coredns", "-n", "kube-system"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return "Restarted CoreDNS"
    return f"CoreDNS restart failed: {result.stderr.strip()}"


def flux_reconcile(kind: str, name: str, namespace: str = "flux-system") -> str:
    result = subprocess.run(
        ["flux", "reconcile", kind, name, "-n", namespace],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        return f"Flux reconciled {kind}/{name} in {namespace}"
    return f"Flux reconcile failed: {result.stderr.strip()}"


def flux_suspend(kind: str, name: str, namespace: str = "flux-system") -> str:
    result = subprocess.run(
        ["flux", "suspend", kind, name, "-n", namespace],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Suspended Flux {kind}/{name} in {namespace}"
    return f"Flux suspend failed: {result.stderr.strip()}"


def flux_resume(kind: str, name: str, namespace: str = "flux-system") -> str:
    result = subprocess.run(
        ["flux", "resume", kind, name, "-n", namespace],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return f"Resumed Flux {kind}/{name} in {namespace}"
    return f"Flux resume failed: {result.stderr.strip()}"
