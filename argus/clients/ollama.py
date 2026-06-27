import json
import httpx


class OllamaClient:
    def __init__(self, url: str, model: str, api_key: str = ""):
        self.url = url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def ask(self, prompt: str) -> str:
        r = httpx.post(
            f"{self.url}/chat/completions",
            headers=self._headers(),
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            verify=False,
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def stream(self, prompt: str):
        """Yield content tokens as they arrive via SSE."""
        with httpx.stream(
            "POST",
            f"{self.url}/chat/completions",
            headers=self._headers(),
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
            },
            verify=False,
            timeout=120,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.startswith("data: ") and not line.endswith("[DONE]"):
                    try:
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except Exception:
                        pass
