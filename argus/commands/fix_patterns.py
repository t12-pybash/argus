import re
from dataclasses import dataclass


@dataclass
class Pattern:
    name: str
    keywords: list[str]
    description: str
    use_llm: bool = False  # True = matched but still needs LLM (e.g. CrashLoopBackOff)


PATTERNS: list[Pattern] = [
    Pattern(
        name="ImagePullBackOff",
        keywords=["imagepullbackoff", "errimagepull", "back-off pulling image", "backoff pulling image"],
        description="Container image cannot be pulled — auth failure or image not found",
    ),
    Pattern(
        name="OOMKilled",
        keywords=["oomkilled", "exit 137", "signal: killed"],
        description="Container killed by kernel OOM — memory limit too low",
    ),
    Pattern(
        name="Evicted",
        keywords=["evicted", "the node was low on resource"],
        description="Pod evicted due to node resource pressure",
    ),
    Pattern(
        name="CrashLoopBackOff",
        keywords=["crashloopbackoff", "back-off restarting failed container"],
        description="Container repeatedly crashing — needs log analysis to determine root cause",
        use_llm=True,
    ),
]


def match(signal: str, events: str, describe: str) -> Pattern | None:
    """Return the first matching Pattern, or None if no known pattern matches."""
    text = f"{signal} {events} {describe}".lower()
    for pattern in PATTERNS:
        if any(kw in text for kw in pattern.keywords):
            return pattern
    return None


def render(pattern: Pattern, namespace: str, pod: str, describe: str) -> str:
    """Generate deterministic fix YAML for a matched pattern."""
    deployment = _pod_to_deployment(pod)

    if pattern.name == "ImagePullBackOff":
        return _fix_image_pull(namespace, deployment, describe)
    if pattern.name == "OOMKilled":
        return _fix_oom(namespace, deployment, describe)
    if pattern.name == "Evicted":
        return _fix_evicted(namespace, deployment)
    return ""


# ── helpers ──────────────────────────────────────────────────────────────────

def _pod_to_deployment(pod: str) -> str:
    """Strip ReplicaSet and pod hash suffixes: argus-9b4b79477-c574q → argus."""
    name = re.sub(r'-[a-f0-9]{7,10}-[a-z0-9]{5}$', '', pod)
    name = re.sub(r'-[a-z0-9]{5}$', '', name)
    return name


def _parse_memory_limit(describe: str) -> str:
    """Extract memory limit from kubectl describe output."""
    match = re.search(r'(?i)limits:.*?memory:\s+([0-9]+[A-Za-z]+)', describe, re.DOTALL)
    if match:
        return match.group(1)
    # Fallback: any memory: value
    match = re.search(r'memory:\s+([0-9]+[A-Za-z]+)', describe)
    return match.group(1) if match else "512Mi"


def _double_memory(mem: str) -> str:
    """Double a Kubernetes memory value: 512Mi → 1Gi, 1Gi → 2Gi."""
    m = re.match(r'^(\d+)(Mi|Gi|Ki|M|G|K)$', mem.strip(), re.IGNORECASE)
    if not m:
        return "1Gi"
    val, unit = int(m.group(1)) * 2, m.group(2)
    if unit.upper() == "MI" and val >= 1024:
        return f"{val // 1024}Gi"
    return f"{val}{unit}"


def _container_name(describe: str, fallback: str) -> str:
    """Extract the first container name from describe output."""
    m = re.search(r'^  ([a-z][a-z0-9\-]+):\s*$', describe, re.MULTILINE)
    return m.group(1) if m else fallback


def _image_ref(describe: str) -> tuple[str, str]:
    """Return (registry, secret_name) inferred from the image in describe output."""
    m = re.search(r'Image:\s+(\S+)', describe)
    image = m.group(1) if m else ""
    if "ghcr.io" in image:
        return "ghcr.io", "ghcr-pull-secret"
    if "registry.gitlab.com" in image:
        return "registry.gitlab.com", "gitlab-pull-secret"
    if "docker.io" in image or "/" not in image:
        return "docker.io", "dockerhub-pull-secret"
    return "ghcr.io", "ghcr-pull-secret"  # safe default for this stack


# ── fix templates ─────────────────────────────────────────────────────────────

def _fix_image_pull(namespace: str, deployment: str, describe: str) -> str:
    registry, secret_name = _image_ref(describe)
    return f"""\
# Pattern: ImagePullBackOff
# Root cause: {registry} pull secret '{secret_name}' is missing or expired.
#
# ACTION REQUIRED — recreate the secret before merging this MR:
#   kubectl create secret docker-registry {secret_name} \\
#     -n {namespace} \\
#     --docker-server={registry} \\
#     --docker-username=<username> \\
#     --docker-password=<PAT or token> \\
#     --dry-run=client -o yaml | kubectl apply -f -
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {deployment}
  namespace: {namespace}
spec:
  template:
    spec:
      imagePullSecrets:
        - name: {secret_name}
"""


def _fix_oom(namespace: str, deployment: str, describe: str) -> str:
    current = _parse_memory_limit(describe)
    new_limit = _double_memory(current)
    container = _container_name(describe, deployment)
    return f"""\
# Pattern: OOMKilled
# Root cause: Container exceeded memory limit ({current}).
# Fix: Doubled memory limit → {new_limit}.
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {deployment}
  namespace: {namespace}
spec:
  template:
    spec:
      containers:
        - name: {container}
          resources:
            requests:
              memory: "{current}"
            limits:
              memory: "{new_limit}"
"""


def _fix_evicted(namespace: str, deployment: str) -> str:
    return f"""\
# Pattern: Evicted
# Root cause: Node resource pressure (disk or memory).
# Fix: Add explicit resource requests so the scheduler can place the pod
#      on a node with sufficient headroom.
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {deployment}
  namespace: {namespace}
spec:
  template:
    spec:
      containers:
        - name: {deployment}
          resources:
            requests:
              memory: "128Mi"
              cpu: "100m"
            limits:
              memory: "512Mi"
              cpu: "500m"
"""
