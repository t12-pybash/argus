from rich.console import Console
from rich.markdown import Markdown
from sre.clients.prometheus import PrometheusClient
from sre.clients.ollama import OllamaClient
from sre import config as cfg_module

console = Console()


def ask(question: str):
    cfg = cfg_module.load()
    prom = PrometheusClient(cfg["prometheus_url"])
    ollama = OllamaClient(cfg["ollama_url"], cfg["ollama_model"])

    context_parts = []

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
            lines = [f"- {r['metric'].get('namespace')}/{r['metric'].get('pod')}: {r['value'][1]} restarts" for r in restarts]
            context_parts.append("High-restart pods:\n" + "\n".join(lines))
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
