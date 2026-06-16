from rich.console import Console
from rich.markdown import Markdown
from sre.clients.prometheus import PrometheusClient
from sre.clients.ollama import OllamaClient
from sre.clients.kubernetes import (
    running_pods_by_namespace, format_pods_context,
    pvcs, format_pvcs_context,
    problem_pods, format_problem_pods_context,
    deployment_readiness, format_deployment_readiness_context,
    flux_sync_status, format_flux_context,
    pod_exit_reason,
)
from sre import config as cfg_module

console = Console()


def ask(question: str):
    cfg = cfg_module.load()
    prom = PrometheusClient(cfg["prometheus_url"])
    ollama = OllamaClient(cfg["ollama_url"], cfg["ollama_model"], cfg.get("ollama_api_key", ""))

    context_parts = []
    ip_map = {}
    try:
        ip_map = prom.node_ip_map()
    except Exception:
        pass

    try:
        nodes = prom.nodes_up()
        node_lines = [f"- {n['metric'].get('instance')} ({n['metric'].get('job')}): {'UP' if n['value'][1]=='1' else 'DOWN'}" for n in nodes]
        context_parts.append("Nodes:\n" + "\n".join(node_lines))
    except Exception:
        pass

    try:
        alerts = prom.firing_alerts()
        if alerts:
            alert_lines = [f"- {a['metric'].get('alertname')}" for a in alerts]
            context_parts.append("Firing alerts:\n" + "\n".join(alert_lines))
        else:
            context_parts.append("Firing alerts: none")
    except Exception:
        pass

    try:
        restarts = prom.pod_restarts()
        if restarts:
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
            context_parts.append("High-restart pods:\n" + "\n".join(lines))
    except Exception:
        pass

    try:
        cpu = prom.node_cpu_usage()
        if cpu:
            lines = [f"- {ip_map.get(r['metric'].get('instance'), r['metric'].get('instance'))}: {float(r['value'][1]):.1f}%" for r in cpu]
            context_parts.append("Node CPU usage:\n" + "\n".join(lines))
    except Exception:
        pass

    try:
        mem = prom.node_memory_usage()
        if mem:
            lines = [f"- {ip_map.get(r['metric'].get('instance'), r['metric'].get('instance'))}: {float(r['value'][1]):.1f}%" for r in mem]
            context_parts.append("Node memory usage:\n" + "\n".join(lines))
    except Exception:
        pass

    try:
        ns_pods = running_pods_by_namespace()
        if ns_pods:
            context_parts.append(format_pods_context(ns_pods))
    except Exception:
        pass

    try:
        pvc_list = pvcs()
        context_parts.append(format_pvcs_context(pvc_list))
    except Exception:
        pass

    try:
        prob = problem_pods()
        context_parts.append(format_problem_pods_context(prob))
    except Exception:
        pass

    try:
        deps = deployment_readiness()
        context_parts.append(format_deployment_readiness_context(deps))
    except Exception:
        pass

    try:
        disk = prom.node_disk_usage()
        if disk:
            lines = [f"- {ip_map.get(r['metric'].get('instance'), r['metric'].get('instance'))}: {float(r['value'][1]):.1f}%" for r in disk]
            context_parts.append("Node disk usage (/):\n" + "\n".join(lines))
    except Exception:
        pass

    try:
        flux = flux_sync_status()
        context_parts.append(format_flux_context(flux))
    except Exception:
        pass

    context = "\n\n".join(context_parts)
    prompt = f"""You are an SRE assistant for a homelab Kubernetes cluster running on kubeadm.
Current cluster state:
{context}

Question: {question}

Answer concisely and practically."""

    with console.status("[cyan]Thinking..."):
        response = ollama.ask(prompt)

    console.print(Markdown(response))
