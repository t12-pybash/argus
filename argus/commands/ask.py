from concurrent.futures import ThreadPoolExecutor
from rich.console import Console
from argus.clients.prometheus import PrometheusClient
from argus.clients.ollama import OllamaClient
from argus.clients.kubernetes import (
    running_and_problem_pods, format_pods_context, format_problem_pods_context,
    pvcs, format_pvcs_context,
    deployment_readiness, format_deployment_readiness_context,
    flux_sync_status, format_flux_context,
    pod_exit_reason,
)
from argus import config as cfg_module

console = Console()


def ask(question: str):
    cfg = cfg_module.load()
    prom = PrometheusClient(cfg["prometheus_url"])
    ollama = OllamaClient(cfg["ollama_url"], cfg["ollama_model"], cfg.get("ollama_api_key", ""))

    ip_map = {}
    try:
        ip_map = prom.node_ip_map()
    except Exception:
        pass

    def _nodes():
        nodes = prom.nodes_up()
        lines = [f"- {n['metric'].get('instance')} ({n['metric'].get('job')}): {'UP' if n['value'][1]=='1' else 'DOWN'}" for n in nodes]
        return "Nodes:\n" + "\n".join(lines)

    def _alerts():
        alerts = prom.firing_alerts()
        if alerts:
            return "Firing alerts:\n" + "\n".join(f"- {a['metric'].get('alertname')}" for a in alerts)
        return "Firing alerts: none"

    def _restarts():
        restarts = prom.pod_restarts()
        if not restarts:
            return None
        lines = []
        for r in restarts:
            ns = r['metric'].get('namespace', '')
            pod = r['metric'].get('pod', '')
            count = r['value'][1]
            try:
                reason = pod_exit_reason(ns, pod)
            except Exception:
                reason = ""
            suffix = f" [{reason}]" if reason else ""
            lines.append(f"- {ns}/{pod}: {count} restarts{suffix}")
        return "High-restart pods:\n" + "\n".join(lines)

    def _cpu():
        cpu = prom.node_cpu_usage()
        if not cpu:
            return None
        lines = [f"- {ip_map.get(r['metric'].get('instance'), r['metric'].get('instance'))}: {float(r['value'][1]):.1f}%" for r in cpu]
        return "Node CPU usage:\n" + "\n".join(lines)

    def _mem():
        mem = prom.node_memory_usage()
        if not mem:
            return None
        lines = [f"- {ip_map.get(r['metric'].get('instance'), r['metric'].get('instance'))}: {float(r['value'][1]):.1f}%" for r in mem]
        return "Node memory usage:\n" + "\n".join(lines)

    def _disk():
        disk = prom.node_disk_usage()
        if not disk:
            return None
        lines = [f"- {ip_map.get(r['metric'].get('instance'), r['metric'].get('instance'))}: {float(r['value'][1]):.1f}%" for r in disk]
        return "Node disk usage (/):\n" + "\n".join(lines)

    def _pods_and_problems():
        running, problems = running_and_problem_pods()
        parts = []
        if running:
            parts.append(format_pods_context(running))
        parts.append(format_problem_pods_context(problems))
        return "\n\n".join(parts)

    def _pvcs():
        return format_pvcs_context(pvcs())

    def _deployments():
        return format_deployment_readiness_context(deployment_readiness())

    def _flux():
        return format_flux_context(flux_sync_status())

    tasks = [_nodes, _alerts, _restarts, _cpu, _mem, _disk, _pods_and_problems, _pvcs, _deployments, _flux]

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(t) for t in tasks]

    context_parts = []
    for f in futures:
        try:
            result = f.result()
            if result:
                context_parts.append(result)
        except Exception:
            pass

    context = "\n\n".join(context_parts)
    prompt = f"""You are an SRE assistant for a homelab Kubernetes cluster running on kubeadm.
Current cluster state:
{context}

Question: {question}

Answer concisely and practically."""

    console.print("[dim cyan]→[/dim cyan] ", end="")
    full = ""
    try:
        for token in ollama.stream(prompt):
            print(token, end="", flush=True)
            full += token
    except Exception:
        full = ollama.ask(prompt)
        print(full)
    print()
