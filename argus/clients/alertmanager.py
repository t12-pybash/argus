import httpx


class AlertmanagerClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def alerts(self) -> list:
        r = httpx.get(f"{self.url}/api/v2/alerts", verify=False, timeout=10)
        r.raise_for_status()
        return r.json()
