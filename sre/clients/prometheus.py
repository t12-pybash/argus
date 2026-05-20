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
