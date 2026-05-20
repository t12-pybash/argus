import httpx


class OllamaClient:
    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model

    def ask(self, prompt: str) -> str:
        r = httpx.post(
            f"{self.url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["response"]
