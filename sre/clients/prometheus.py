import httpx


class PrometheusClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def query(self, promql: str) -> list:
        r = httpx.get(f"{self.url}/api/v1/query", params={"query": promql}, verify=False, timeout=10)
        r.raise_for_status()
        return r.json()["data"]["result"]

    def nodes_up(self) -> list:
        return self.query('up{job="node-exporter"}')

    def firing_alerts(self) -> list:
        return self.query("ALERTS{alertstate='firing'}")

    def pod_restarts(self) -> list:
        return self.query("sum by(namespace,pod)(kube_pod_container_status_restarts_total) > 5")

    def node_ip_map(self) -> dict:
        """Returns {ip: node_name} from kube_node_info."""
        results = self.query('kube_node_info')
        mapping = {}
        for r in results:
            node = r['metric'].get('node', '')
            ip = r['metric'].get('internal_ip', '')
            if node and ip:
                mapping[f"{ip}:9100"] = node
        return mapping

    def node_cpu_usage(self) -> list:
        return self.query('100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)')

    def node_memory_usage(self) -> list:
        return self.query('100 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes * 100)')
