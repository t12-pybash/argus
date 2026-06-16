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
